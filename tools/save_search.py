"""Create a Facebook Marketplace saved search in the bot's own browser profile.

    python tools/save_search.py --dry-run     # show what the page offers, click nothing
    python tools/save_search.py               # create the saved search
    python tools/save_search.py --check       # read the notifications page and report

Why this exists: a saved search is the only way to get Facebook to *push* a
listing to us instead of us polling for it. Whether that push actually beats
our 30s poll is an open question — --check is how we measure it.

The profile is shared with the watcher, so stop the watcher first (the
dashboard's Stop button, or POST /api/stop). Chromium refuses to open a
profile directory that another browser process already holds.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Facebook's labels carry £ and U+FFFC (the object-replacement char it uses for
# inline icons). A cp1252 console raises on those, which would kill the run
# halfway through a dump.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.async_api import Page, async_playwright  # noqa: E402

from watcher import config as config_loader  # noqa: E402
from watcher.browser import _USER_AGENT  # noqa: E402
from watcher.models import AppConfig  # noqa: E402

DEBUG_DIR = Path("data/debug")

# Facebook's class names are obfuscated and rotate, so every lookup here is by
# visible text or ARIA role. Each list is tried in order; the first hit wins.
# Facebook renamed "Save search" to "Notify Me" (bell icon, under the search
# box). The old labels are kept first-to-last as fallbacks in case the account
# or the rollout is on the older UI.
SAVE_BUTTON_TEXTS = ("Notify Me", "Save search", "Save this search")
# What the button reads once the search is saved and notifications are on.
SAVED_BUTTON_TEXTS = ("Notifications on", "Notifying", "Saved", "Notify Me")
CREATE_BUTTON_TEXTS = ("Create Alert", "Create", "Save", "Done", "Turn on")
DIALOG = 'div[role="dialog"]'


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


async def _clickable_by_text(page: Page, texts: tuple[str, ...], *, root: str | None = None):
    """Return the first visible clickable element whose label matches `texts`.

    Matches on exact text first, then on a case-insensitive prefix, so
    "Save search" is not shadowed by an unrelated "Save" elsewhere on the page.
    """
    scope = page.locator(root) if root else page.locator("body")
    candidates = scope.locator(
        'div[role="button"], a[role="button"], button, a[href], span[role="button"]'
    )
    count = await candidates.count()
    exact: list = []
    loose: list = []
    for i in range(count):
        el = candidates.nth(i)
        try:
            if not await el.is_visible():
                continue
            label = _norm(await el.inner_text())
            aria = _norm(await el.get_attribute("aria-label"))
        except Exception:
            continue
        for want in texts:
            w = want.lower()
            if label.lower() == w or aria.lower() == w:
                exact.append((want, el))
                break
            if label.lower().startswith(w) or aria.lower().startswith(w):
                loose.append((want, el))
                break
    for want, el in exact + loose:
        return want, el
    return None, None


async def _dump_buttons(page: Page, label: str) -> None:
    """Log every visible button label — the fallback when a selector misses."""
    print(f"\n  --- visible buttons ({label}) ---")
    seen: list[str] = []
    candidates = page.locator('div[role="button"], a[role="button"], button, a[href]')
    for i in range(min(await candidates.count(), 400)):
        el = candidates.nth(i)
        try:
            if not await el.is_visible():
                continue
            text = _norm(await el.inner_text())
            aria = _norm(await el.get_attribute("aria-label"))
            shown = text or aria
            if shown and shown not in seen:
                seen.append(shown)
        except Exception:
            continue
    for s in seen:
        print(f"    {s[:90]!r}")
    print(f"  --- {len(seen)} distinct labels ---\n")


async def _visible_dialog(page: Page):
    """Return the first *visible* role=dialog, or None.

    Facebook keeps several dialogs mounted but hidden (a PIN prompt among
    them), so counting them is not the same as one being on screen.
    """
    dialogs = page.locator(DIALOG)
    for i in range(await dialogs.count()):
        el = dialogs.nth(i)
        try:
            if await el.is_visible():
                return el
        except Exception:
            continue
    return None


async def _shot(page: Page, name: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"saved-search-{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
        print(f"  screenshot: {path}")
    except Exception as exc:  # a screenshot must never be the thing that fails
        print(f"  (screenshot failed: {exc})")


async def create_saved_search(cfg: AppConfig, *, dry_run: bool) -> int:
    url = cfg.search.build_url()
    print(f"\n  Search URL: {url}\n")

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=cfg.user_data_dir,
                headless=False,
                user_agent=_USER_AGENT,
                viewport={"width": 1400, "height": 1000},
                locale="en-GB",
                timezone_id="Europe/London",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            print(
                "\n  Could not open the browser profile.\n"
                f"  {exc}\n\n"
                "  If the watcher is running it is holding this profile. Stop it\n"
                "  (dashboard Stop button, or POST /api/stop) and try again.\n"
            )
            return 1

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        if await page.query_selector('input[name="email"]') is not None:
            print("\n  Landed on a login wall — the stored session has expired.")
            print("  Run: python main.py --login\n")
            await context.close()
            return 2

        await _shot(page, "1-results")

        want, button = await _clickable_by_text(page, SAVE_BUTTON_TEXTS)
        if button is None:
            print("  No 'Save search' button found on the results page.")
            await _dump_buttons(page, "results page")
            await context.close()
            return 3

        print(f"  Found button: {want!r}")
        if dry_run:
            print("  --dry-run: not clicking anything.")
            await _dump_buttons(page, "results page")
            await context.close()
            return 0

        await button.click()
        await page.wait_for_timeout(2500)
        await _shot(page, "2-after-click")

        dialog = await _visible_dialog(page)
        if dialog is None:
            # Some rollouts save in one click with no dialog. The only evidence
            # of success is the button relabelling itself.
            want2, _ = await _clickable_by_text(page, SAVED_BUTTON_TEXTS)
            print(f"  No dialog. Button now reads: {want2!r}")
            if want2 and want2.lower() != want.lower():
                print("\n  Saved search created (one-click flow).")
                await context.close()
                return 0
            print("\n  No dialog and the button did not change — nothing was saved.")
            await _dump_buttons(page, "after save click")
            await context.close()
            return 4

        # The dialog carries the search term and location from the page, but
        # NOT the price range — the URL's minPrice/maxPrice are dropped. Fill
        # them so the alert matches what the watcher actually filters on.
        for placeholder, value in (
            ("Min", cfg.search.min_price),
            ("Max", cfg.search.max_price),
        ):
            if value is None:
                continue
            field = dialog.locator(f'input[placeholder*="{placeholder}" i]').first
            if await field.count() == 0:
                print(f"  No {placeholder} price field in the dialog.")
                continue
            await field.fill(str(value))
            print(f"  Set {placeholder} price: {value}")

        await _shot(page, "3-filled")

        want, create = await _clickable_by_text(page, CREATE_BUTTON_TEXTS, root=DIALOG)
        if create is None:
            print("  No Create/Save button inside the dialog.")
            await _dump_buttons(page, "dialog")
            await context.close()
            return 5

        print(f"  Clicking {want!r}")
        await create.click()
        await page.wait_for_timeout(3000)
        await _shot(page, "4-after-create")

        still_open = await _visible_dialog(page) is not None
        print(
            "\n  Dialog still open — creation probably failed."
            if still_open
            else "\n  Dialog closed — saved search created."
        )

        want2, _ = await _clickable_by_text(page, SAVED_BUTTON_TEXTS)
        print(f"  Button now reads: {want2!r}")

        await page.wait_for_timeout(1500)
        await context.close()
        return 0 if not still_open else 6


async def check_notifications(cfg: AppConfig) -> int:
    """Read the notifications page and print Marketplace entries with ages."""
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=cfg.user_data_dir,
            headless=False,
            user_agent=_USER_AGENT,
            viewport={"width": 1400, "height": 1000},
            locale="en-GB",
            timezone_id="Europe/London",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.facebook.com/notifications", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await _shot(page, "5-notifications")

        rows = page.locator('a[href*="/marketplace/item/"], a[href*="saved_search"]')
        n = await rows.count()
        print(f"\n  {n} Marketplace-ish notification links\n")
        for i in range(min(n, 25)):
            el = rows.nth(i)
            try:
                text = _norm(await el.inner_text())
                href = await el.get_attribute("href")
            except Exception:
                continue
            if text:
                print(f"    {text[:120]!r}\n      {href}")

        if n == 0:
            await _dump_buttons(page, "notifications page")

        await context.close()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Marketplace saved search in the bot profile.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect the page, click nothing")
    parser.add_argument("--check", action="store_true", help="Read the notifications page instead")
    parser.add_argument("--config", help="Path to an alternative config file")
    args = parser.parse_args()

    cfg = config_loader.load(args.config)
    if args.check:
        return asyncio.run(check_notifications(cfg))
    return asyncio.run(create_saved_search(cfg, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
