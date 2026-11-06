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
    log.info("Dashboard on http://127.0.0.1:%d — Ctrl+C to stop", port)
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
        print(f"\nConfiguration problem:\n  {exc}\n", file=sys.stderr)
        return 1

    try:
        cfg = cfg.with_overrides(
            headless=None if args.headless is None else args.headless == "true",
            dashboard_port=args.port if args.dashboard else None,
            debug=args.debug or None,
        )
    except ValueError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

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
