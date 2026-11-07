"""Navigate a Marketplace search and parse listing cards.

All Facebook-specific selectors live in SELECTORS. Class names are obfuscated
and change regularly, so nothing here depends on them: the stable anchor is the
listing link itself, and each field falls back to a second strategy.
"""

from __future__ import annotations

import logging
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
_LISTED_DETAIL = re.compile(
    r"Listed\s+(just now|today|yesterday|\d+\s*(?:second|minute|hour|day|week)s?\s+ago)",
    re.IGNORECASE,
)


async def open_search(session: BrowserSession, cfg: AppConfig) -> Page:
    """Open the configured search URL and confirm we landed on results."""
    page = await session.new_page()
    url = cfg.search.build_url()
    # Facebook serves this page from an edge cache. A unique parameter makes
    # each poll a distinct request, so a poll can never be handed a snapshot
    # older than the one before it. Facebook ignores the parameter itself.
    url += ("&" if "?" in url else "?") + f"_cb={int(time.time() * 1000)}"
    log.debug("Navigating to %s", url)

    try:
        await page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeout as exc:
        await page.close()
        raise RuntimeError(f"Timed out loading search page: {url}") from exc

    await session.assert_logged_in(page)

    try:
        await page.wait_for_selector(SELECTORS["listing_anchor"], timeout=15000)
    except PlaywrightTimeout:
        # Not fatal by itself — an empty result set for a narrow search is
        # legitimate. The caller decides whether zero listings is suspicious.
        log.debug("No listing anchors appeared within 15s")

    return page


async def _card_lines(anchor) -> list[str]:
    """Return the card's text as discrete lines.

    Deliberately not `innerText`: it joins sibling inline spans with a *space*,
    turning "£280" + "Croydon, London" into one "£280 Croydon, London" line, so
    the location could never be told apart from the price. Querying the leaf
    spans keeps each value separate, which is what the field parsers need.
    One round-trip per card, since Facebook's cards are text-in-spans.
    """
    lines = await anchor.evaluate(
        """el => {
            let node = el;
            for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                if (/£\\s*\\d/.test(node.innerText || '')) break;
            }
            if (!node) return [];
            const leaves = [...node.querySelectorAll('span')]
                .filter(s => !s.querySelector('span'));
            return leaves
                .map(s => (s.textContent || '').trim())
                .filter(t => t.length > 0);
        }"""
    )
    return lines or []


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

    Returns (listings, parsed_count) where parsed_count counts anchors seen,
    so the caller can tell "no anchor broke" from "no cards found".
    """
    anchors = await page.query_selector_all(SELECTORS["listing_anchor"])
    listings: list[Listing] = []
    seen: set[str] = set()
    unparsed = 0

    for anchor in anchors:
        if len(listings) >= limit:
            break
        href = await anchor.get_attribute("href") or ""
        match = _ITEM_ID.search(href)
        if not match:
            continue
        item_id = match.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)

        lines = await _card_lines(anchor)
        if not lines:
            unparsed += 1
            continue

        price = None
        for line in lines:
            price_match = _PRICE.search(line)
            if price_match:
                price = int(price_match.group(1).replace(",", ""))
                break

        image_alt = ""
        img = await anchor.query_selector(SELECTORS["card_image_alt"])
        if img is not None:
            image_alt = (await img.get_attribute("alt") or "").strip()

        title = _parse_title(lines, image_alt)
        if not title:
            unparsed += 1
            continue

        location = _parse_location(lines)
        listings.append(
            Listing.from_card(
                item_id=item_id,
                title=_strip_location_suffix(title, location),
                price=price,
                location=location,
                listed_text=_parse_listed(lines),
            )
        )

    return listings, unparsed


async def fetch_listed_age(session: BrowserSession, url: str) -> str:
    """Read a listing's real "Listed X ago" wording from its detail page.

    The search card badges everything "Just listed", so the card alone cannot
    tell you how old a listing is. Only worth doing for listings we are about
    to alert on — it costs one extra page load.
    """
    page = await session.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        text = await page.inner_text("body")
    except Exception as exc:  # noqa: BLE001 - a missing age must not block the alert
        log.warning("Could not read listed age for %s: %s", url, exc)
        return ""
    finally:
        await page.close()

    match = _LISTED_DETAIL.search(text)
    if not match:
        return ""
    raw = match.group(1).lower().strip()
    return "just now" if raw in ("just listed", "just now") else raw


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
