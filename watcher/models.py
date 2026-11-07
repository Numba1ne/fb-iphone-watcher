"""Immutable data types passed between modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# "5 minutes ago" / "2 hours ago" / "3 days ago", as Facebook words it.
_AGO = re.compile(r"(\d+)\s*(second|minute|hour|day|week)s?\s+ago", re.IGNORECASE)
_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}


def _estimate_listed_at(listed_text: str, now: datetime) -> str | None:
    """Turn Facebook's relative wording into an absolute UTC timestamp.

    Relative text is all the card exposes, so this is an estimate anchored to
    the moment we scraped it — good to the granularity Facebook itself gives
    ("2 hours ago" is only accurate to the hour).
    """
    if not listed_text:
        return None
    low = listed_text.lower().strip()
    if low in ("just now", "just listed", "today"):
        base = now.replace(hour=0, minute=0, second=0, microsecond=0) if low == "today" else now
        return base.isoformat(timespec="seconds")
    if low == "yesterday":
        yesterday = now - timedelta(days=1)
        return yesterday.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")
    match = _AGO.search(low)
    if match:
        delta = int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        return (now - timedelta(seconds=delta)).isoformat(timespec="seconds")
    return None


@dataclass(frozen=True)
class Listing:
    """A single Marketplace listing, normalised from a scraped card."""

    id: str
    title: str
    price: int | None
    location: str
    url: str
    # Facebook's own wording, e.g. "just now", "2 hours ago", "yesterday".
    listed_text: str = ""
    # The same moment expressed absolutely, estimated at scrape time.
    listed_at: str | None = None

    @classmethod
    def from_card(
        cls,
        item_id: str,
        title: str,
        price: int | None,
        location: str,
        listed_text: str = "",
        now: datetime | None = None,
    ) -> "Listing":
        # Rebuild the URL from the id rather than trusting the scraped href, so
        # tracking params and localisation prefixes never leak into the alert.
        moment = now or datetime.now(timezone.utc)
        return cls(
            id=item_id,
            title=title.strip(),
            price=price,
            location=location.strip() or "Unknown location",
            url=f"https://www.facebook.com/marketplace/item/{item_id}",
            listed_text=listed_text,
            listed_at=_estimate_listed_at(listed_text, moment),
        )

    @property
    def price_text(self) -> str:
        return "Price unknown" if self.price is None else f"£{self.price:,}"

    @property
    def listed_display(self) -> str:
        """Human wording for alerts and the dashboard."""
        return self.listed_text or "unknown"

    def with_listed_text(self, listed_text: str, now: datetime | None = None) -> "Listing":
        """Return a copy carrying a more accurate listed time.

        The search card only ever says "Just listed"; the detail page carries
        the real age, so this is how a card gets corrected after the fact.
        """
        moment = now or datetime.now(timezone.utc)
        return replace(
            self,
            listed_text=listed_text,
            listed_at=_estimate_listed_at(listed_text, moment),
        )


@dataclass(frozen=True)
class SearchSpec:
    """Everything needed to open a Marketplace search page."""

    city_slug: str
    query: str
    min_price: int | None
    max_price: int | None
    radius_km: int
    latitude: float
    longitude: float
    # Facebook only honours `sortBy=creation_time_descend` when a recency
    # window is present: without it the feed returns listings ~15 hours old
    # even though the cards are badged "Just listed". None/0 = no window.
    days_since_listed: int | None = 1
    override_url: str = ""

    def build_url(self) -> str:
        """Return the search URL, or the user's override verbatim."""
        if self.override_url:
            return self.override_url

        # `exact=false` widens matching so listings titled "Apple iPhone 13"
        # are still returned. Newest-first keeps new items in the first screen.
        params = {
            "query": self.query,
            "sortBy": "creation_time_descend",
            "exact": "false",
            "radiusKM": str(self.radius_km),
            "lat": f"{self.latitude}",
            "lon": f"{self.longitude}",
        }
        if self.min_price is not None:
            params["minPrice"] = str(self.min_price)
        if self.max_price is not None:
            params["maxPrice"] = str(self.max_price)
        if self.days_since_listed:
            params["daysSinceListed"] = str(self.days_since_listed)

        return (
            f"https://www.facebook.com/marketplace/{self.city_slug}/search/"
            f"?{urlencode(params)}"
        )


@dataclass(frozen=True)
class AppConfig:
    search: SearchSpec
    interval_seconds: float
    jitter_seconds: float
    max_cards: int
    restart_browser_every: int
    headless: bool
    user_data_dir: str
    navigation_timeout_ms: int
    require_any: tuple[str, ...]
    exclude: tuple[str, ...]
    telegram: bool
    discord: bool
    first_run_silent: bool
    db_path: str
    log_path: str
    debug_dir: str
    telegram_token: str
    telegram_chat_id: str
    discord_webhook_url: str
    debug: bool = False
    # Measurement only: read the notifications page every N polls and record
    # when each listing first appears there, so the saved-search push can be
    # compared against the poll. Off by default — it costs an extra page load.
    notifications_probe: bool = False
    notifications_probe_every: int = 4

    def with_overrides(
        self,
        *,
        headless: bool | None = None,
        dashboard_port: int | None = None,
        debug: bool | None = None,
    ) -> "AppConfig":
        """Return a copy with CLI overrides applied.

        The dashboard port isn't stored on AppConfig; it is validated here so
        a typo fails fast instead of binding somewhere unexpected.
        """
        from dataclasses import replace

        if dashboard_port is not None and not (1024 <= dashboard_port <= 65535):
            raise ValueError(
                f"--port {dashboard_port} is out of range; use 1024–65535."
            )
        return replace(
            self,
            headless=self.headless if headless is None else headless,
            debug=self.debug if debug is None else debug,
        )
