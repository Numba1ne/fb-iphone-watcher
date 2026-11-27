"""Open the watcher's own browser profile so the Facebook session can be renewed.

The watcher reuses one Chromium profile directory, so the session lives or dies
with it. When it expires nothing crashes: the search page still returns
listings, anonymously, from a more heavily cached edge, and only the
notifications page bounces to a login form. The bot therefore looks healthy
while being minutes behind, which is why signing in has to be *verified* rather
than assumed.

Reached from `python main.py --login` and `python tools/login.py`. Nothing here
scrapes anything: it opens a window, waits, and reports whether a usable
session exists, so a checkpoint or a password prompt is Dave's to clear by hand.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import _USER_AGENT
from .models import AppConfig

# Where to stand while signing in: the notifications page is the one the probe
# needs, so a redirect here is exactly the symptom being fixed, and it bounces
# to the login form on its own when there is no session.
LOGIN_URL = "https://www.facebook.com/notifications"
LOGIN_MARKERS = ("/login", "checkpoint", "login.php")
EMAIL_INPUT = 'input[name="email"]'
DEFAULT_TIMEOUT_S = 600.0
POLL_MS = 1500


async def has_session(context) -> tuple[bool, str]:
    """Return (logged_in, reason) for the profile's Facebook cookie jar."""
    try:
        jar = {c["name"] for c in await context.cookies("https://www.facebook.com")}
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        return False, f"could not read cookies: {exc}"
    if "c_user" not in jar:
        return False, "no c_user cookie (signed out)"
    if "xs" not in jar:
        return False, "c_user present but xs missing (session not actually usable)"
    return True, "c_user and xs present"


async def _on_login_wall(page) -> bool:
    if any(marker in page.url.lower() for marker in LOGIN_MARKERS):
        return True
    try:
        return await page.query_selector(EMAIL_INPUT) is not None
    except Exception:  # noqa: BLE001 - mid-navigation the page can be blank
        return False


async def sign_in(cfg: AppConfig, timeout_s: float = DEFAULT_TIMEOUT_S) -> int:
    """Open the profile headed and wait until a real session is stored.

    Returns a process exit code: 0 when the profile ends up signed in and the
    notifications page loads, 1 otherwise.
    """
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

        ok, reason = await has_session(context)
        if ok and not await _on_login_wall(page):
            print(f"\n  Already signed in ({reason}) and the notifications page loads.")
            print("  Nothing to do. Restart the watcher if it is running.")
            return 0

        print(f"\n  Not signed in: {reason}.")
        print("  A browser window is open. Sign in to Facebook in it now, and")
        print("  clear any checkpoint or 'confirm it's you' screen as well.")
        print(f"  Waiting up to {int(timeout_s)} seconds...\n")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await page.wait_for_timeout(POLL_MS)
            ok, reason = await has_session(context)
            if ok and not await _on_login_wall(page):
                break
        else:
            ok = False
        if not ok:
            print(f"\n  Gave up: {reason}. Current page: {page.url}")
            print("  Nothing was saved. Re-run this once the login completes.")
            return 1

        # Cookies are not proof on their own: a checkpoint screen can leave
        # them behind, so load the page the probe actually uses.
        try:
            await page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
            await page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            print(f"  Verification navigation did not settle: {exc}")
        if await _on_login_wall(page):
            print(f"\n  Signed in, but {LOGIN_URL} still shows a login wall: {page.url}")
            print("  Finish whatever Facebook is asking for, then re-run this.")
            return 1

        print("\n  Signed in and the notifications page loads. Session saved.")
        print("  Start the watcher again now.")
        return 0
    finally:
        await context.close()
        await playwright.stop()
