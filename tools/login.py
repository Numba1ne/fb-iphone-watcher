"""Open the watcher's own browser profile so Dave can sign in to Facebook.

    python tools/login.py

Why this exists: the watcher reuses one Chromium profile directory, so the
session lives or dies with it. When it expires the search page still returns
listings — anonymously — so nothing crashes and nothing looks wrong, but the
results come from Facebook's logged-out cache and the notifications page just
redirects to a login form. The bot then looks healthy while being minutes
behind. This reuses the same profile, headed, and waits for a real session.

The profile is single-use while Chromium holds it, so stop the watcher first
(the dashboard's Stop button, or POST /api/stop).

Nothing here scrapes anything. It opens a window, waits, and reports whether a
session exists, so a checkpoint or a password prompt is Dave's to clear by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Facebook's pages carry characters a cp1252 console cannot encode.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a redirected stream may not support it
        pass

from playwright.async_api import async_playwright  # noqa: E402

from watcher import config as config_loader  # noqa: E402
from watcher.browser import _USER_AGENT  # noqa: E402

LOGIN_URL = "https://www.facebook.com/notifications"
# Where to stand while Dave signs in: the notifications page is the one the
# probe needs, so a redirect here is exactly the symptom being fixed, and it
# bounces to the login form on its own when there is no session.
LOGIN_MARKERS = ("/login", "checkpoint", "login.php")
EMAIL_INPUT = 'input[name="email"]'
TIMEOUT_S = 600.0
POLL_MS = 1500


async def _has_session(context) -> tuple[bool, str]:
    """Return (logged_in, reason)."""
    try:
        jar = {c["name"] for c in await context.cookies("https://www.facebook.com")}
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read cookies: {exc}"
    if "c_user" not in jar:
        return False, "no c_user cookie (signed out)"
    if "xs" not in jar:
        return False, "c_user present but xs missing (session not actually usable)"
    return True, "c_user and xs present"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT_S,
        help="seconds to wait for a sign-in (default 600)",
    )
    args = parser.parse_args()

    cfg = config_loader.load()
    Path(cfg.user_data_dir).mkdir(parents=True, exist_ok=True)

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=cfg.user_data_dir,
        headless=False,
        user_agent=_USER_AGENT,
        viewport={"width": 1400, "height": 1000},
        locale="en-GB",
        timezone_id="Europe/London",
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
        except Exception as exc:  # noqa: BLE001 - a slow shell is not a failure
            print(f"  Navigation did not settle ({exc}); reading what arrived.")

        ok, reason = await _has_session(context)
        if ok:
            print(f"\n  Already signed in ({reason}). Nothing to do.")
            return 0

        print(f"\n  Not signed in: {reason}.")
        print("  A browser window is open. Sign in to Facebook in it now.")
        print("  Clear any checkpoint or 'confirm it's you' screen as well.")
        print("  Waiting up to %d seconds...\n" % int(args.timeout))

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            await page.wait_for_timeout(POLL_MS)
            ok, reason = await _has_session(context)
            if ok:
                break
        if not ok:
            print(f"\n  Gave up: {reason}. Current page: {page.url}")
            print("  Nothing was saved. Re-run this once the login completes.")
            return 1

        # A cookie alone is not proof the notifications page renders: a
        # checkpoint screen can leave cookies behind. Load it and check where
        # we actually landed.
        try:
            await page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
            await page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            print(f"  Verification navigation did not settle: {exc}")
        url = page.url.lower()
        on_wall = any(marker in url for marker in LOGIN_MARKERS)
        if not on_wall:
            try:
                on_wall = await page.query_selector(EMAIL_INPUT) is not None
            except Exception:  # noqa: BLE001
                on_wall = False
        if on_wall:
            print(f"\n  Signed in, but {LOGIN_URL} still shows a login wall: {page.url}")
            print("  Finish whatever Facebook is asking for, then re-run this.")
            return 1

        print("\n  Signed in and the notifications page loads. Session saved.")
        print("  Restart the watcher now:")
        print("    launchctl kickstart -k gui/$(id -u)/com.fbiphone.watcher")
        return 0
    finally:
        await context.close()
        await playwright.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
