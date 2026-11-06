"""Facebook Marketplace iPhone watcher — CLI entry point.

    python main.py --login          one-time manual Facebook login
    python main.py --test-notify    send a sample alert to each channel
    python main.py --once --debug   single poll, dump HTML, then exit
    python main.py                  run the watcher loop
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
import sys
import time

from watcher import config as config_loader
from watcher import filters
from watcher.browser import BrowserSession, SessionExpired
from watcher.logging_setup import configure as configure_logging
from watcher.marketplace import dump_debug, open_search, parse_cards
from watcher.models import AppConfig
from watcher.notify import Notifier
from watcher.store import Store

log = logging.getLogger("watcher")

_SHUTDOWN = False


def _handle_stop(signum: int, frame: object) -> None:
    global _SHUTDOWN  # noqa: PLW0603 - module-level signal flag, by design
    log.info("Stop requested — finishing the current poll then exiting")
    _SHUTDOWN = True


async def _poll_once(cfg: AppConfig, session: BrowserSession, *, debug: bool) -> tuple[int, int]:
    """Run one scrape-and-alert cycle. Returns (parsed, new)."""
    page = await open_search(session, cfg)
    try:
        cards, unparsed = await parse_cards(page, cfg.max_cards)
        if not cards and debug:
            await dump_debug(page, cfg.debug_dir, "no-cards")
        if unparsed:
            log.warning("%d card(s) could not be parsed", unparsed)
            if debug:
                await dump_debug(page, cfg.debug_dir, "unparsed")
    finally:
        await page.close()

    matched = filters.apply(
        cards,
        require_any=cfg.require_any,
        exclude=cfg.exclude,
        min_price=cfg.search.min_price,
        max_price=cfg.search.max_price,
    )

    with Store(cfg.db_path) as store:
        first_run = store.is_empty
        silent = first_run and cfg.first_run_silent
        if first_run:
            log.info("First run — recording %d existing listing(s) silently", len(matched))

        new_count = 0
        async with Notifier(cfg) as notifier:
            for listing in matched:
                # Claim first, notify second: if we crash in between we lose one
                # alert rather than sending the same listing on every restart.
                if not store.claim(listing, now=cfg.now, alert=not silent):
                    continue
                new_count += 1
                log.info(
                    "NEW  %s  %s  (%s)", listing.price_text, listing.title, listing.location
                )
                if not silent:
                    await notifier.send(listing)

    return len(cards), new_count


async def run_loop(cfg: AppConfig, *, once: bool = False, debug: bool = False) -> int:
    polls = 0
    failures = 0
    session = BrowserSession(cfg)
    await session.start()

    try:
        while not _SHUTDOWN:
            started = time.monotonic()
            try:
                parsed, new = await _poll_once(cfg, session, debug=debug)
                failures = 0
                log.info("Poll #%d: parsed=%d new=%d", polls + 1, parsed, new)
            except SessionExpired as exc:
                log.error("Session expired: %s", exc)
                async with Notifier(cfg) as notifier:
                    await notifier.alert_session_expired(str(exc))
                return 2
            except Exception:  # noqa: BLE001 - a single bad poll must not end a long run
                failures += 1
                log.exception("Poll failed (%d consecutive)", failures)

            polls += 1
            if once:
                return 0

            if failures >= 5:
                log.error("5 consecutive failures — restarting browser")
                await session.restart()
                failures = 0
            elif cfg.restart_browser_every and polls % cfg.restart_browser_every == 0:
                await session.restart()

            elapsed = time.monotonic() - started
            wait = cfg.interval_seconds - elapsed
            wait += random.uniform(-cfg.jitter_seconds, cfg.jitter_seconds)
            wait = max(5.0, wait)
            log.debug("Sleeping %.1fs", wait)
            await _sleep(wait)
    finally:
        await session.stop()

    return 0


async def _sleep(seconds: float) -> None:
    """Sleep in short slices so Ctrl+C is honoured promptly."""
    deadline = time.monotonic() + seconds
    while not _SHUTDOWN and time.monotonic() < deadline:
        await asyncio.sleep(min(0.5, deadline - time.monotonic()))


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

    print("\n  Login saved to the browser profile. Next: python main.py --once\n")
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

    configure_logging(cfg.log_path, verbose=args.verbose or args.debug)
    if args.headless is not None:
        cfg = _replace_headless(cfg, args.headless == "true")

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
        return asyncio.run(run_loop(cfg, once=args.once, debug=args.debug))
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 0


def _replace_headless(cfg: AppConfig, headless: bool) -> AppConfig:
    from dataclasses import replace

    return replace(cfg, headless=headless)


if __name__ == "__main__":
    sys.exit(main())
