"""Navigate a Marketplace search and parse listing cards.

All Facebook-specific selectors live in SELECTORS. Class names are obfuscated
and change regularly, so nothing here depends on them: the stable anchor is the
listing link itself, and each field falls back to a second strategy.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .browser import BrowserSession
from .models import AppConfig, Listing

log = logging.getLogger(__name__)

SELECTORS = {
    # Any anchor whose href contains this is a listing card (or detail link).
    "listing_anchor": 'a[href*="/marketplace/item/"]',
    "card_image_alt": "img[alt]",
    # Region that groups the whole page's result cards.
    "results_container": '[aria-label*="arketplace"], [role="main"]',
}

_ITEM_ID = re.compile(r"/marketplace/item/(\d+)")
_PRICE = re.compile(r"£\s*([\d,]+)")
# "listed" only counts as a badge when a time follows it ("Listed 2 hours ago",
# "Listed just now"); a title that merely starts with the word is a real title.
_NON_PRICE_LINE = re.compile(
    r"^(£|free$|sponsored|just listed|listed\s+(?:just|today|yesterday|\d)|sold)",
    re.IGNORECASE,
)
# The "Just listed" / "Listed 2 hours ago" badge. Anchored to the whole line so
# a title that merely contains the word "listed" is not read as a timestamp.
_LISTED_BADGE = re.compile(
    r"^(?:listed\s+)?"
    r"(just listed|just now|today|yesterday|\d+\s*(?:second|minute|hour|day|week)s?\s+ago)"
    r"$",
    re.IGNORECASE,
)
# The detail page states the real age ("Listed 41 minutes ago in Sunderland"),
# unlike the card, which badges everything "Just listed".
# Every listing the page renders also carries its exact posting moment in the
# JSON that draws the card: ...,"id":"1156701640250336"},"creation_time":1794214628.
# This is the card's own data, so it costs no extra request, and it beats the
# detail page's wording, which is only accurate to the hour.
_CREATION_TIME = re.compile(r'"id":"(\d{6,})"\},"creation_time":(\d{10})')
# Facebook's clock as it rendered the page. Used to warn about a skewed local
# clock, which would otherwise silently mis-judge every age.
_SERVER_TIME = re.compile(r'"server_time":(\d{10})')


def _bust_cache(url: str) -> str:
    """Return `url` with unique, ignorable parameters so nothing is reused.

    Facebook's search page comes from an edge cache. A whole-millisecond
    timestamp can collide when two searches fire in the same tick, so a
    fractional-time nonce and a random value are added too. Facebook ignores
    all of them; they exist only to make every request distinct.
    """
    sep = "&" if "?" in url else "?"
    nonce = f"{time.time():.6f}".replace(".", "")
    return (
        f"{url}{sep}_cb={int(time.time() * 1000)}"
        f"&_nonce={nonce}&_r={random.randrange(1_000_000)}"
    )


async def open_search(
    session: BrowserSession, cfg: AppConfig, *, url: str | None = None
) -> Page:
    """Open a search URL and confirm we landed on results.

    Always a brand-new page: nothing is reused between polls, so no rendering
    state, scroll position or in-page cache survives into the next request.
    The caller owns the returned page and must close it.
    """
    page = await session.new_page()
    target = _bust_cache(url or cfg.search.build_url())
    log.debug("Navigating to %s", target)

    try:
        await page.goto(target, wait_until="domcontentloaded")
    except PlaywrightTimeout as exc:
        await page.close()
        raise RuntimeError(f"Timed out loading search page: {target}") from exc

    await session.assert_logged_in(page)

    try:
        # Short on purpose: this only decides how long to wait for the first
        # card before parsing anyway. At 15s an empty secondary search added a
        # quarter of a minute to every poll.
        await page.wait_for_selector(SELECTORS["listing_anchor"], timeout=8000)
    except PlaywrightTimeout:
        # Not fatal by itself — an empty result set for a narrow search is
        # legitimate. The caller decides whether zero listings is suspicious.
        log.debug("No listing anchors appeared within 8s")

    return page


_COLLECT_JS = r"""
(limit) => {
    const out = [];
    const seen = new Set();
    for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
        const m = (a.getAttribute('href') || '').match(/\/marketplace\/item\/(\d+)/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        if (out.length >= limit) break;
        let node = a;
        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
            if (/£\s*\d/.test(node.innerText || '')) break;
        }
        if (!node) continue;
        const leaves = [...node.querySelectorAll('span')]
            .filter(s => !s.querySelector('span'))
            .map(s => (s.textContent || '').trim())
            .filter(t => t.length > 0);
        const img = a.querySelector('img[alt]');
        out.push({
            id: id,
            lines: leaves,
            alt: img ? (img.getAttribute('alt') || '') : ''
        });
    }
    return out;
}
"""


def _parse_title(text_lines: list[str], image_alt: str) -> str:
    if image_alt and "photo" not in image_alt.lower():
        return image_alt.strip()
    # Fallback: the longest line that isn't a price, location or label.
    candidates = [
        line.strip()
        for line in text_lines
        if line.strip()
        and not _PRICE.search(line)
        and not _NON_PRICE_LINE.match(line.strip())
        and len(line.strip()) > 3
        # "Sold"/"Just listed" style badges carry a dot separator; drop them.
        and not _TRAILING_META.fullmatch(line.strip())
        and not any(char.isdigit() for char in line[:1])
    ]
    return max(candidates, key=len, default="")


_TRAILING_META = re.compile(r"\s*(·.*|listed .*|sponsored.*)$", re.IGNORECASE)


def _parse_listed(text_lines: list[str]) -> str:
    """Return the card's "listed" badge as normalised wording, or "".

    Facebook words this as "Just listed", "Listed 5 minutes ago", "Listed
    yesterday" and so on. "Just listed" and "Just now" collapse to "just now"
    so downstream code has one spelling to reason about.
    """
    for line in text_lines:
        match = _LISTED_BADGE.match(line.strip())
        if match:
            raw = match.group(1).lower().strip()
            return "just now" if raw in ("just listed", "just now") else raw
    return ""


def _parse_location(text_lines: list[str]) -> str:
    """Find the location, which sits after the price on its own or same line.

    Handles both shapes Facebook uses: separate spans ("£280", "Croydon") and
    one span ("£280Croydon, London" / "£280 Croydon, London").

    A card reads badge, price, title, location — so the location is the *last*
    usable line. Taking the first line after the price returns the title, which
    is what made every listing appear to be located in its own name.
    """
    for index, line in enumerate(text_lines):
        price_match = _PRICE.search(line)
        if not price_match:
            continue

        # Prices inside the card are always the first slide; anything after on
        # the same line is the location.
        tail = line[price_match.end():].strip().lstrip("·, -").strip()
        if tail and not _NON_PRICE_LINE.match(tail):
            return _TRAILING_META.sub("", tail).strip()

        for following in reversed(text_lines[index + 1 :]):
            candidate = following.strip()
            if not candidate:
                continue
            if _PRICE.search(candidate) or _NON_PRICE_LINE.match(candidate):
                continue
            cleaned = _TRAILING_META.sub("", candidate).strip()
            if cleaned:
                return cleaned
    return ""


def _strip_location_suffix(title: str, location: str) -> str:
    """Drop the " in <location>" tail Facebook appends to the image alt text.

    The alt reads "iPhone 16 Pro in Brighton and Hove" while the card's own
    title line is just "iPhone 16 Pro"; keeping the tail duplicates the Area
    column and makes the title harder to scan.
    """
    if not location:
        return title
    suffix = f" in {location}"
    if title.lower().endswith(suffix.lower()):
        return title[: -len(suffix)].strip()
    return title


async def parse_cards(page: Page, limit: int) -> tuple[list[Listing], int]:
    """Extract listings from the current page.

    Returns (listings, unparsed) where `unparsed` counts cards whose text could
    not be read, so the caller can tell "the page changed" from "no cards".

    Every card is collected in a single evaluate() instead of one round-trip
    per card. The old per-anchor loop cost ~2 round-trips per card, which on a
    24-card page was most of the poll's wall time — and poll latency is the
    thing standing between us and a 4-minute-old listing.
    """
    rows = await page.evaluate(_COLLECT_JS, limit)
    listings: list[Listing] = []
    unparsed = 0

    for row in rows or []:
        lines = row.get("lines") or []
        if not lines:
            unparsed += 1
            continue

        price = None
        for line in lines:
            price_match = _PRICE.search(line)
            if price_match:
                price = int(price_match.group(1).replace(",", ""))
                break

        title = _parse_title(lines, row.get("alt") or "")
        if not title:
            unparsed += 1
            continue

        location = _parse_location(lines)
        listings.append(
            Listing.from_card(
                item_id=str(row["id"]),
                title=_strip_location_suffix(title, location),
                price=price,
                location=location,
                listed_text=_parse_listed(lines),
            )
        )

    return listings, unparsed


def parse_creation_times(html: str) -> tuple[dict[str, int], int | None]:
    """Map each listing id on the page to its exact posting epoch.

    Returns (id -> epoch seconds, Facebook's server time or None). A listing
    missing from the map is one whose timestamp the page did not carry; the
    caller must treat that as "age unknown", never as "brand new".
    """
    posted = {str(item_id): int(stamp) for item_id, stamp in _CREATION_TIME.findall(html)}
    server = _SERVER_TIME.search(html)
    return posted, (int(server.group(1)) if server else None)


async def read_creation_times(page: Page) -> tuple[dict[str, int], int | None]:
    """Read the page's own timestamps. Never raises: an age is not worth a poll."""
    try:
        return parse_creation_times(await page.content())
    except Exception as exc:  # noqa: BLE001 - a missing age must not fail the poll
        log.warning("Could not read listing timestamps from the page: %s", exc)
        return {}, None


def stamp_created(listings: list[Listing], posted: dict[str, int], now_epoch: int) -> list[Listing]:
    """Attach the exact posting moment to every card that has one."""
    return [
        listing.with_created(posted[listing.id], now_epoch=now_epoch)
        if listing.id in posted
        else listing
        for listing in listings
    ]


async def dump_debug(page: Page, debug_dir: str, reason: str) -> Path:
    """Save a screenshot and the page HTML so a failed parse is diagnosable."""
    out = Path(debug_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    html_path = out / f"{stamp}-{reason}.html"
    shot_path = out / f"{stamp}-{reason}.png"

    html_path.write_text(await page.content(), encoding="utf-8")
    try:
        await page.screenshot(path=str(shot_path), full_page=True)
    except Exception:  # noqa: BLE001 - a screenshot failure must not mask the real error
        log.warning("Could not capture screenshot for debug dump")
    log.info("Debug dump written to %s", html_path)
    return html_path
