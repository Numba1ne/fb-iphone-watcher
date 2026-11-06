"""Facebook Marketplace iPhone watcher — CLI entry point.

    python main.py                  watch in the terminal
    python main.py --dashboard      open the web dashboard (starts watching too)
    python main.py --login          one-time manual Facebook login
    python main.py --test-notify    send a sample alert to each channel
    python main.py --once --debug   single poll, dump HTML, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from watcher import config as config_loader
from watcher.logging_setup import configure as configure_logging
from watcher.models import AppConfig
from watcher.notify import Notifier
from watcher.runner import WatcherRunner

log = logging.getLogger("watcher")

_SHUTDOWN = False


def _handle_stop(signum: int, frame: object) -> None:
    global _SHUTDOWN  # noqa: PLW0603 - module-level signal flag, by design
    log.info("Stop requested — finishing the current poll then exiting")
    _SHUTDOWN = True


async def run_terminal(cfg: AppConfig, *, once: bool = False, debug: bool = False) -> int:
    """No-dashboard mode: run the loop until Ctrl+C or the session expires."""
    runner = WatcherRunner(cfg)
    await runner.start()

    while not _SHUTDOWN:
        await asyncio.sleep(0.25)
        status = runner.status
        if status.state == "failed":
            await runner.stop()
            return 2
        if once and status.poll_count >= 1:
            await runner.stop()
            return 0
        if not runner.running:
            return 0

    await runner.stop()
    return 0


async def run_dashboard(cfg: AppConfig, *, port: int) -> int:
    from watcher.web import serve

    runner = WatcherRunner(cfg)
    await serve(cfg, runner, port=port)
    return 0


async def do_login(cfg: AppConfig) -> int:
    """Open a visible browser for the user to log in to Facebook by hand."""
    from playwright.async_api import async_playwright

    from watcher.browser import _USER_AGENT  # reuse the exact same identity

    print("\n  A browser window will open. Log in to Facebook normally, wait for")
    print("  the Marketplace page to load, then close the window.\n")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=cfg.user_data_dir,
            headless=False,
            user_agent=_USER_AGENT,
            locale="en-GB",
            timezone_id="Europe/London",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.facebook.com/marketplace/" + cfg.search.city_slug)
        await context.pages[-1].wait_for_event("close", timeout=0)
        await context.close()

    print("\n  Login saved to the browser profile. Next: python main.py\n")
    return 0


async def do_test_notify(cfg: AppConfig) -> int:
    async with Notifier(cfg) as notifier:
        ok = await notifier.send_test()
    if ok:
        log.info("Test notification sent — check Telegram and Discord")
        return 0
    log.error("Test notification failed — see the errors above")
    return 1


def _port_available(port: int) -> bool:
    """True if nothing is already bound to the dashboard port on loopback."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # On Windows SO_REUSEADDR lets a second bind succeed silently, so a
        # reuse-based check always says "free". Exclusive-use is the only
        # option that actually reports a port already in service.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _explain_and_wait(message: str, exit_code: int) -> int:
    """Print a problem in a way that survives a double-clicked window.

    Launched from Explorer, the console vanishes the moment the process exits,
    which makes a terse config error look like "the dashboard just doesn't
    work". Pause only on a real console so piped/CI runs don't hang.
    """
    print(message, file=sys.stderr, flush=True)
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close…")
        except (EOFError, KeyboardInterrupt):
            pass
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Facebook Marketplace for new iPhone listings.")
    parser.add_argument("--dashboard", action="store_true", help="Run the web dashboard (also starts watching)")
    parser.add_argument("--port", type=int, default=8765, help="Dashboard port (default 8765)")
    parser.add_argument("--login", action="store_true", help="Open a browser to log in to Facebook")
    parser.add_argument("--test-notify", action="store_true", help="Send a sample alert")
    parser.add_argument("--once", action="store_true", help="Poll once then exit")
    parser.add_argument("--debug", action="store_true", help="Dump HTML when parsing looks wrong")
    parser.add_argument("--headless", choices=["true", "false"], help="Override headless setting")
    parser.add_argument("--config", help="Path to an alternative config file")
    parser.add_argument("--verbose", action="store_true", help="Log at DEBUG level")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cfg = config_loader.load(args.config)
    except config_loader.ConfigError as exc:
        hint = ""
        if args.dashboard:
            hint = (
                "\nThe dashboard did not start, so the browser has nothing to connect to.\n"
                f"Fix the problem above, then run this again."
            )
        return _explain_and_wait(f"\nConfiguration problem:\n  {exc}\n{hint}", 1)

    try:
        cfg = cfg.with_overrides(
            headless=None if args.headless is None else args.headless == "true",
            dashboard_port=args.port if args.dashboard else None,
            debug=args.debug or None,
        )
    except ValueError as exc:
        return _explain_and_wait(f"\n{exc}\n", 1)

    if args.dashboard and not _port_available(args.port):
        return _explain_and_wait(
            f"\nPort {args.port} is already in use — another copy may already be running.\n"
            f"Open http://127.0.0.1:{args.port} to check, or start this one with "
            f"--port {args.port + 1}.\n",
            1,
        )

    configure_logging(cfg.log_path, verbose=args.verbose or args.debug)

    if args.login:
        return asyncio.run(do_login(cfg))
    if args.test_notify:
        return asyncio.run(do_test_notify(cfg))

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

    log.info(
        "Watching '%s' within %dkm · £%s–£%s · every %.0fs",
        cfg.search.query,
        cfg.search.radius_km,
        cfg.search.min_price if cfg.search.min_price is not None else "0",
        cfg.search.max_price if cfg.search.max_price is not None else "∞",
        cfg.interval_seconds,
    )
    log.debug("Search URL: %s", cfg.search.build_url())

    try:
        if args.dashboard:
            return asyncio.run(run_dashboard(cfg, port=args.port))
        return asyncio.run(run_terminal(cfg, once=args.once, debug=args.debug))
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
