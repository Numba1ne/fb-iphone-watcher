"""Pure listing filters — no browser or network needed, so they stay testable."""

from __future__ import annotations

import re

from .models import Listing


def _compile_exclusions(terms: tuple[str, ...]) -> list[re.Pattern[str]]:
    """Build case-insensitive word-boundary matchers for exclusion terms.

    The `(?<!un)` guard is load-bearing: without it the bare term "locked"
    would reject "unlocked", which describes most of the good listings.
    """
    patterns = []
    for term in terms:
        escaped = re.escape(term.lower())
        patterns.append(
            re.compile(rf"(?<!un)\b{escaped}\b", re.IGNORECASE)
        )
    return patterns


def matches(listing: Listing, *, require_any: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    """True when a listing should trigger a notification."""
    title = listing.title.lower()

    if require_any and not any(term.lower() in title for term in require_any):
        return False

    if listing.price is not None and listing.price <= 0:
        # Free or nonsensical prices are usually mis-typed or bait.
        return False

    return not any(pattern.search(title) for pattern in _compile_exclusions(exclude))


def apply(
    listings: list[Listing],
    *,
    require_any: tuple[str, ...],
    exclude: tuple[str, ...],
    min_price: int | None = None,
    max_price: int | None = None,
) -> list[Listing]:
    """Filter a batch, enforcing the price band locally too.

    Facebook treats `maxPrice`/`minPrice` as real filters, but we re-check
    locally so a silently-ignored URL parameter can't flood the alerts.
    """
    result = []
    for listing in listings:
        if not matches(listing, require_any=require_any, exclude=exclude):
            continue
        if listing.price is None:
            # Keep unpriced listings only if the band is open; a listing with
            # no parsable price can't be judged against it.
            if min_price is not None or max_price is not None:
                continue
        else:
            if min_price is not None and listing.price < min_price:
                continue
            if max_price is not None and listing.price > max_price:
                continue
        result.append(listing)
    return result
