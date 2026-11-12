"""Run the saved-search notification probe once, and show what it found.

    python tools/probe.py
    python tools/probe.py --dump          # also save the page HTML + screenshot

The watcher already runs this probe every `probe_every` polls, but it only
says "N listing(s) visible" in the log. When that number is 0 forever there
are two very different explanations — no saved-search alert has fired, or the
notifications page no longer looks the way the extractor expects — and the
log cannot tell them apart. This tool can: it reports the counts for each
candidate selector, so an empty page reads differently from a page full of
notifications we are failing to parse.

It does not record anything and does not alert. It is a look, nothing more.

The Facebook profile is single-use while Chromium holds it, so stop the
watcher (dashboard Stop, or close the terminal) before running this.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# A Windows console inherits cp1252, which cannot encode the em dashes below.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - a redirected stream may not support it
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import config as config_loader  # noqa: E402
from watcher.browser import BrowserSession  # noqa: E402
from watcher.notifications import (  # noqa: E402
    NOTIFICATIONS_URL,
    _EXTRACT_JS,
    _open_notifications,
    fetch_alert_items,
)

# Every plausible way the notifications page could present a listing. Reported
# side by side so the mismatch is visible rather than guessed at.
_SURVEY_JS = """
() => {
    const count = (sel) => document.querySelectorAll(sel).length;
    const sample = (sel, n) => Array.from(document.querySelectorAll(sel))
        .slice(0, n)
        .map(el => ((el.getAttribute('href') || el.innerText || '').trim()))
        .map(s => s.replace(/\\s+/g, ' ').slice(0, 120));
    return {
        item_anchors: count('a[href*="/marketplace/item/"]'),
        marketplace_anchors: count('a[href*="marketplace"]'),
        item_id_anchors: count('a[href*="item_id="]'),
        listitems: count('[role="listitem"]'),
        rows: count('[role="row"]'),
        notifications_path: (location.pathname + location.search).slice(0, 200),
        title: (document.title || '').slice(0, 120),
        body_len: (document.body ? document.body.innerText.length : 0),
        marketplace_samples: sample('a[href*="marketplace"]', 12),
        item_id_samples: sample('a[href*="item_id="]', 12),
        body_head: (document.body ? document.body.innerText : '')
            .replace(/\\s+/g, ' ').slice(0, 600),
    };
}
"""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def probe(cfg, *, dump: bool, wait_ms: int) -> int:
    print(f"\n  Notifications URL: {NOTIFICATIONS_URL}")
    print(f"  Profile:           {cfg.user_data_dir}")
    print(f"  Settling {wait_ms / 1000:.0f}s after the list renders\n")

    session = BrowserSession(cfg)
    try:
        await session.start()
    except Exception as exc:  # noqa: BLE001 - the common failure is a locked profile
        print(f"  Could not start the browser: {exc}\n")
        print("  If the watcher is running, it is holding the profile. Stop it")
        print("  (dashboard Stop button) and run this again.\n")
        return 2

    page = None
    try:
        # The real extractor, so the numbers below are the ones the loop sees.
        items = await fetch_alert_items(session)

        try:
            page = await session.new_page()
            # The watcher's own open path, so this survey reports what the poll
            # loop actually sees rather than what a kinder wait would.
            await _open_notifications(page)
            await page.wait_for_timeout(wait_ms)
            survey = await page.evaluate(_SURVEY_JS)
            rows = await page.evaluate(_EXTRACT_JS)

            if dump:
                debug_dir = Path(cfg.debug_dir)
                debug_dir.mkdir(parents=True, exist_ok=True)
                stem = debug_dir / f"probe-{_stamp()}"
                Path(f"{stem}.html").write_text(
                    await page.content(), encoding="utf-8"
                )
                try:
                    await page.screenshot(path=f"{stem}.png", full_page=True)
                except Exception as exc:  # noqa: BLE001 - a screenshot is a nicety
                    print(f"  (screenshot failed: {exc})")
                print(f"  Dumped page to {stem}.html / .png\n")
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not read the notifications page: {exc}\n")
            return 1

        print("  What the page contains")
        print("  " + "-" * 58)
        print(f"  landed on:            {survey['notifications_path']}")
        print(f"  document title:       {survey['title']}")
        print(f"  visible text length:  {survey['body_len']} chars")
        print(f"  a[href*=marketplace/item/]:  {survey['item_anchors']}")
        print(f"  a[href*=marketplace]:        {survey['marketplace_anchors']}")
        print(f"  a[href*=item_id=]:           {survey['item_id_anchors']}")
        print(f"  [role=listitem]:             {survey['listitems']}")
        print(f"  [role=row]:                  {survey['rows']}")

        if survey["marketplace_samples"]:
            print("\n  Marketplace links on the page")
            print("  " + "-" * 58)
            for sample in survey["marketplace_samples"]:
                print(f"    {sample}")
        if survey["item_id_samples"]:
            print("\n  item_id links on the page")
            print("  " + "-" * 58)
            for sample in survey["item_id_samples"]:
                print(f"    {sample}")

        print("\n  What the extractor returns")
        print("  " + "-" * 58)
        print(f"  listings:             {len(items)}")
        print(f"  raw rows:             {len(rows)}")
        for item in items:
            print(f"    id={item['id']:<18} age={item['age_text'] or '?':<6} {item['text'][:70]}")

        print("\n  Verdict")
        print("  " + "-" * 58)
        if items:
            print(f"  The probe works: {len(items)} listing(s) are visible in notifications.")
            print("  Run `python tools/notify_race.py` after the watcher has polled")
            print("  to see whether the push beat the poll.")
        elif survey["item_id_anchors"]:
            print("  The page has Marketplace links but the extractor matched none.")
            print("  That is a selector problem, not a quiet account — compare the")
            print("  link shapes above against the regex in watcher/notifications.py.")
        elif survey["body_len"] < 500:
            print("  The page came back nearly empty — probably a login wall or a")
            print("  render that had not finished. See the text head below.")
            print(f"\n  body: {survey['body_head']}")
        else:
            print("  The page rendered and holds no Marketplace listings: no")
            print("  saved-search alert has fired for this account.")
            print("  Create one at facebook.com/marketplace/<city>/saved (or from a")
            print("  search page: 'Save search'), and alerts will land here.")
            print(f"\n  body: {survey['body_head']}")
        print()

        # Zero here while the survey found listing links is the failure worth
        # exiting non-zero on, so this composes in a script.
        if not items and survey["item_id_anchors"]:
            return 1
        return 0
    finally:
        if page is not None:
            await page.close()
        await session.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the saved-search notification probe once and report."
    )
    parser.add_argument("--config", help="Path to an alternative config file")
    parser.add_argument(
        "--dump", action="store_true", help="Save the page HTML and a screenshot"
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=6000,
        help="How long to wait for the notification list to render (default 6000)",
    )
    args = parser.parse_args()

    # The probe's own warnings ("probe failed", "list never rendered") explain
    # a run that finds nothing, so they belong on the console and not only in
    # the watcher's log file.
    logging.basicConfig(
        level=logging.WARNING, format="  %(levelname)s  %(name)s: %(message)s"
    )

    try:
        cfg = config_loader.load(args.config)
    except config_loader.ConfigError as exc:
        print(f"\nConfiguration problem:\n  {exc}\n")
        return 1

    if not cfg.notifications_probe:
        print(
            "\n  notifications.probe is false in config.yaml.\n"
            "  This tool still works, but the watcher will not run the probe.\n"
        )

    return asyncio.run(probe(cfg, dump=args.dump, wait_ms=args.wait_ms))


if __name__ == "__main__":
    sys.exit(main())
