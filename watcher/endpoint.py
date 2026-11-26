"""Direct Marketplace GraphQL endpoint, captured from Facebook itself.

This is deliberately not a "public API". Facebook has no supported Marketplace
feed, so we do not guess query ids: the watcher opens the search page once,
records the GraphQL POST that rendered its results, then replays that same call
on later polls. When Facebook rotates the query id, the next browser restart
captures a fresh one. If the endpoint stops working, the runner falls back to
the ordinary page scrape rather than losing the poll.

The payoff is latency: a GraphQL POST is a few hundred milliseconds where a
full search-page render is several seconds. The freshness gate still does the
important job — Facebook decides when a listing enters the feed, and the bot
refuses anything already old when it finally appears.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .marketplace import open_search
from .models import AppConfig, Listing
from .browser import BrowserSession

log = logging.getLogger(__name__)

_PRICE = re.compile(r"£\s*([\d,]+)")
_TITLE_KEYS = (
    "marketplace_listing_title",
    "listing_title",
    "title",
    "name",
    "custom_title",
)
_LOCATION_KEYS = ("location_text", "listing_location_text", "display_name", "city")
_DROP_HEADERS = {
    "content-length", "host", "connection", "accept-encoding", "cookie",
}


class EndpointError(RuntimeError):
    """Raised when the captured GraphQL call cannot be replayed."""


@dataclass(frozen=True)
class CapturedCall:
    """One GraphQL POST as Facebook's own page issued it."""

    url: str
    form: dict[str, str]
    headers: dict[str, str]
    variables: dict[str, Any]
    friendly_name: str

    @property
    def cache_key(self) -> str:
        return f"{self.friendly_name}:{self.variables.get('count', '')}"


def decode_form(post_data: str | None) -> dict[str, str]:
    """Turn an application/x-www-form-urlencoded body into a flat dict."""
    if not post_data:
        return {}
    parsed = parse_qs(post_data, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def encode_form(form: dict[str, str]) -> str:
    """Reverse decode_form, preserving every Facebook-specific field."""
    return urlencode(form)


def _looks_like_search(variables: dict[str, Any]) -> bool:
    """Reject badges, counts and other GraphQL noise on the same page."""
    markers = ("params", "buyLocation", "savedSearchQuery", "count", "cursor")
    return any(marker in variables for marker in markers)


async def capture_calls(
    session: BrowserSession, cfg: AppConfig, url: str
) -> list[CapturedCall]:
    """Open one search page and record its Marketplace GraphQL POSTs.

    Only requests are recorded — no response parsing happens here. That keeps the
    capture hook synchronous and cheap; the replay step decides which of the
    captured calls actually carries listings.
    """
    found: list[CapturedCall] = []
    seen: set[str] = set()

    def on_request(request: Any) -> None:
        if "/api/graphql" not in request.url:
            return
        form = decode_form(request.post_data)
        friendly = form.get("fb_api_req_friendly_name", "")
        if "MarketplaceSearch" not in friendly:
            return
        raw_variables = form.get("variables")
        if not raw_variables:
            return
        try:
            variables = json.loads(raw_variables)
        except (TypeError, ValueError):
            return
        if not isinstance(variables, dict) or not _looks_like_search(variables):
            return

        key = friendly + ":" + str(variables.get("count", ""))
        if key in seen:
            return
        seen.add(key)
        headers = {
            name: value
            for name, value in (request.headers or {}).items()
            if name.lower() not in _DROP_HEADERS
        }
        found.append(
            CapturedCall(
                url=request.url,
                form=form,
                headers=headers,
                variables=variables,
                friendly_name=friendly,
            )
        )

    async def hook(page: Any) -> None:
        page.on("request", on_request)

    page = await open_search(session, cfg, url=url, on_page=hook)
    try:
        # A slow network can issue the search query just after the first card
        # renders; give it a beat before closing the capture page.
        await page.wait_for_timeout(500)
    finally:
        await page.close()

    found.sort(key=lambda call: "ContentContainer" not in call.friendly_name)
    log.info("Captured %d Marketplace GraphQL call(s)", len(found))
    return found


def variables_for(call: CapturedCall, url: str, *, limit: int) -> dict[str, Any]:
    """Copy the captured variables, then point them at this search's URL.

    The second search differs from the first only in radius and price ceiling,
    so one capture can serve both. Keeping every other field untouched matters:
    Facebook's query ids are paired to an exact variable shape, and inventing
    fields the page did not send is the fastest way to get a 400.
    """
    out = deepcopy(call.variables)
    query = parse_qs(urlsplit(url).query)

    def one(name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    search = one("query")
    if search:
        if "savedSearchQuery" in out:
            out["savedSearchQuery"] = search
        for item in out.get("contextual_data") or []:
            if isinstance(item, dict) and item.get("name") == "search_query":
                item["value"] = json.dumps(search)

    buy = out.get("buyLocation")
    if isinstance(buy, dict):
        lat, lon = one("lat"), one("lon")
        if lat is not None:
            buy["latitude"] = float(lat)
        if lon is not None:
            buy["longitude"] = float(lon)

    if "count" in out and limit > 0:
        out["count"] = min(int(out["count"]), limit)

    params = out.get("params")
    if not isinstance(params, dict):
        return out
    bqf = params.get("bqf")
    if isinstance(bqf, dict) and search:
        bqf["query"] = search
    browse = params.get("browse_request_params")
    if not isinstance(browse, dict):
        return out

    radius, minimum, maximum = one("radiusKM"), one("minPrice"), one("maxPrice")
    if radius is not None:
        browse["filter_radius_km"] = int(float(radius))
    if minimum is not None:
        browse["filter_price_lower_bound"] = int(float(minimum) * 100)
    if maximum is not None:
        browse["filter_price_upper_bound"] = int(float(maximum) * 100)
    elif "filter_price_upper_bound" in browse:
        # The secondary search deliberately drops the ceiling.
        browse["filter_price_upper_bound"] = None
    return out


def _iter_dicts(value: Any):
    """Yield every dict in a nested JSON payload."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _listing_ref(node: dict[str, Any]) -> dict[str, Any] | None:
    """Find the product item inside a search-result node.

    The node's own `id` is a photo id; the listing id lives in the nested
    `GroupCommerceProductItem`. Matching only a numeric id would stamp every
    card with the wrong identifier, so require a product typename or a title.
    """
    for candidate in _iter_dicts(node):
        ident = str(candidate.get("id", ""))
        if not ident.isdigit():
            continue
        typename = str(candidate.get("__typename", ""))
        if "ProductItem" in typename or "MarketplaceListing" in typename:
            return candidate
        if any(isinstance(candidate.get(key), str) and candidate[key].strip() for key in _TITLE_KEYS):
            return candidate
    return None


def _first_string(node: dict[str, Any], keys: tuple[str, ...], *, recursive: bool = False) -> str:
    dictionaries = _iter_dicts(node) if recursive else [node]
    for dictionary in dictionaries:
        for key in keys:
            value = dictionary.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_price(node: dict[str, Any]) -> int | None:
    for dictionary in _iter_dicts(node):
        price = dictionary.get("listing_price")
        if isinstance(price, dict):
            formatted = price.get("formatted_amount")
            if isinstance(formatted, str):
                match = _PRICE.search(formatted)
                if match:
                    return int(match.group(1).replace(",", ""))
            amount = price.get("amount")
            if isinstance(amount, (int, float)) and amount > 1000:
                return int(amount) // 100
        for key in ("price", "formatted_price", "price_text"):
            value = dictionary.get(key)
            if isinstance(value, str):
                match = _PRICE.search(value)
                if match:
                    return int(match.group(1).replace(",", ""))
    return None


def _extract_location(node: dict[str, Any]) -> str:
    for dictionary in _iter_dicts(node):
        reverse = dictionary.get("reverse_geocode")
        if isinstance(reverse, dict):
            for key in ("display_name", "city_page", "city"):
                value = reverse.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    nested = value.get("display_name")
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
    return _first_string(node, _LOCATION_KEYS, recursive=True)


def parse_payload(payload: Any, limit: int) -> tuple[list[Listing], int | None]:
    """Turn a GraphQL response into dated Listing objects.

    Walks the payload rather than hard-coding a path: Facebook moves these nodes
    between `data`, `edges`, and deferred fragments often, but the node's own
    fields stay recognisable. The exact `creation_time` is preferred, so no
    listing is ever treated as brand new just because it has a "Just listed"
    badge.
    """
    server_time: int | None = None
    for dictionary in _iter_dicts(payload):
        for key in ("server_time", "serverTime"):
            value = dictionary.get(key)
            if isinstance(value, (int, float)) and 1_000_000_000 < value < 10_000_000_000:
                server_time = int(value)
                break
        if server_time is not None:
            break

    now = server_time or int(time.time())
    cards: list[Listing] = []
    seen: set[str] = set()
    for dictionary in _iter_dicts(payload):
        posted = dictionary.get("creation_time")
        if not isinstance(posted, (int, float)):
            continue
        if not 1_000_000_000 < posted < 10_000_000_000:
            continue
        ref = _listing_ref(dictionary)
        if ref is None:
            continue
        item_id = str(ref.get("id", ""))
        if not item_id or item_id in seen:
            continue
        title = _first_string(ref, _TITLE_KEYS) or _first_string(dictionary, _TITLE_KEYS, recursive=True)
        if not title:
            continue
        seen.add(item_id)
        card = Listing.from_card(
            item_id=item_id,
            title=title,
            price=_extract_price(dictionary),
            location=_extract_location(dictionary),
        )
        cards.append(card.with_created(int(posted), now_epoch=now))
        if len(cards) >= limit > 0:
            break
    return cards, server_time


class MarketplaceEndpoint:
    """Captures and replays Facebook's own Marketplace search query."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._calls: list[CapturedCall] = []
        self._preferred: CapturedCall | None = None
        self._lock = asyncio.Lock()
        self._session: Any = None

    def invalidate(self) -> None:
        """Forget captured queries after a browser restart."""
        self._calls = []
        self._preferred = None

    async def ensure(self, session: BrowserSession, url: str) -> None:
        # A browser restart hands us a new context; captured queries cannot be
        # replayed against the old one's cookies and tokens.
        if self._session is not session:
            self.invalidate()
            self._session = session
        if self._calls:
            return
        async with self._lock:
            if self._session is not session:
                self.invalidate()
                self._session = session
            if self._calls:
                return
            calls = await capture_calls(session, self.cfg, url)
            if not calls:
                raise EndpointError("No Marketplace GraphQL call was visible on the search page")
            self._calls = calls
            self._preferred = None

    async def fetch(
        self, session: BrowserSession, url: str, limit: int
    ) -> tuple[list[Listing], float]:
        """Return dated listings and the monotonic moment the response arrived."""
        await self.ensure(session, url)
        if not self._calls:
            raise EndpointError("No captured GraphQL call")

        calls = [self._preferred] if self._preferred is not None else []
        calls.extend(call for call in self._calls if call not in calls)
        errors: list[str] = []
        for call in calls:
            form = dict(call.form)
            form["variables"] = json.dumps(
                variables_for(call, url, limit=limit), separators=(",", ":")
            )
            try:
                response = await session.context.request.post(
                    call.url,
                    form=form,
                    headers=call.headers,
                    timeout=self.cfg.navigation_timeout_ms,
                )
                if response.status >= 400:
                    raise EndpointError(f"HTTP {response.status}")
                payload = await response.json()
                cards, _server_time = parse_payload(payload, limit)
                if not cards:
                    raise EndpointError("response held no dated listings")
                self._preferred = call
                return cards, time.monotonic()
            except Exception as exc:  # noqa: BLE001 - try the next captured query
                errors.append(f"{call.friendly_name}: {type(exc).__name__}: {exc}")
        raise EndpointError("; ".join(errors))
