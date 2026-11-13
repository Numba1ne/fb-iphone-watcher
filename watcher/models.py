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


def _wording_for_age(seconds: int) -> str:
    """Phrase an exact age the way Facebook's own wording does.

    Used when the page hands us a real timestamp, so a listing carries the same
    vocabulary as before ("just now", "8 minutes ago") but with the accuracy of
    an epoch instead of a rounded badge.
    """
    seconds = max(0, int(seconds))
    for limit, unit, size in (
        (60, "second", 1),
        (3600, "minute", 60),
        (86400, "hour", 3600),
        (604800, "day", 86400),
        (float("inf"), "week", 604800),
    ):
        if seconds < limit:
            count = seconds // size
            if count <= 0:
                return "just now"
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"  # pragma: no cover - the last branch always returns


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


def age_minutes(listed_text: str) -> int | None:
    """Estimate a listing's age in whole minutes from Facebook's wording.

    "today" and "yesterday" are deliberately mapped to a day: both mean the
    listing is older than any sane freshness threshold, so the gate must read
    them as too old rather than as unknown. None is reserved for wording we
    genuinely cannot parse — that must never be silently treated as zero, or
    a parsing change would alert on the whole backlog.
    """
    if not listed_text:
        return None
    low = listed_text.lower().strip()
    if low in ("just now", "just listed"):
        return 0
    if low == "today":
        return 24 * 60
    if low == "yesterday":
        return 24 * 60
    match = _AGO.search(low)
    if match:
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        return seconds // 60
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
    # The exact moment Facebook says it was posted, in epoch seconds. Read from
    # the search page's own JSON, so it is precise to the second rather than to
    # the badge's wording. None when the page carried no timestamp for this id.
    posted_epoch: int | None = None
    # The clock the page was aged against, in epoch seconds — Facebook's
    # `server_time`, advanced by however long the page took to reach us. Set
    # whenever the page carried its own clock, so a listing's age is measured
    # the same way from the moment it was scraped to the moment it is gated.
    age_clock_epoch: int | None = None

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

    def with_created(self, posted_epoch: int, *, now_epoch: int) -> "Listing":
        """Return a copy carrying the exact moment Facebook posted it.

        This supersedes the card's own "Just listed" badge: the timestamp comes
        from the same payload that draws the card, so it costs no extra request
        and is precise to a second.
        """
        posted = int(posted_epoch)
        return replace(
            self,
            posted_epoch=posted,
            age_clock_epoch=int(now_epoch),
            listed_text=_wording_for_age(now_epoch - posted),
            listed_at=datetime.fromtimestamp(posted, timezone.utc).isoformat(
                timespec="seconds"
            ),
        )

    def aged_now(self, elapsed: float = 0.0) -> "Listing":
        """Advance the listing's age clock by the time since it was stamped.

        A stamp is only the moment the page was dated; the poll then spends
        further seconds parsing, claiming and deciding. Those count, so this
        adds them — `elapsed` is the time since the page was read. Adding the
        gap rather than writing this machine's clock keeps the one thing that
        matters: no clock skew, at any size, moves a listing across the gate.
        """
        if self.age_clock_epoch is None:
            return self
        return replace(self, age_clock_epoch=self.age_clock_epoch + int(elapsed))

    def with_created_clock(self, epoch: int) -> "Listing":
        """Anchor a listing's badge wording to a page's own clock.

        Only for a card the page carried no timestamp for: `epoch` is
        Facebook's `server_time`, so the wording ("2 hours ago") is read
        against the same clock that wrote it rather than this machine's. A
        card that has a timestamp already has its clock, and is left alone —
        re-anchoring it would replace Facebook's clock with this machine's.
        """
        if self.age_clock_epoch is not None:
            return self
        moment = datetime.fromtimestamp(int(epoch), timezone.utc)
        return replace(
            self,
            listed_at=_estimate_listed_at(self.listed_text, moment)
            if self.listed_text
            else None,
            age_clock_epoch=int(epoch),
        )

    def minutes_old(self, *, now_epoch: int | None = None) -> int | None:
        """Age in whole minutes, preferring the exact timestamp.

        Falls back to the wording only when the page gave no timestamp, and to
        None when there is neither — an age we cannot read must never be
        mistaken for "brand new".
        """
        if self.posted_epoch is not None:
            # `now_epoch` when given: the caller knows how long the page took
            # to reach it. Otherwise the moment the page was rendered, which
            # is the same thing minus that delay.
            now = self.age_clock_epoch if now_epoch is None else now_epoch
            return max(0, int(now) - self.posted_epoch) // 60
        return age_minutes(self.listed_text)


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

    def build_url(
        self,
        *,
        radius_km: int | None = None,
        drop_max_price: bool = False,
    ) -> str:
        """Return the search URL, or the user's override verbatim.

        `radius_km` and `drop_max_price` let the runner build a *second* search
        that differs slightly from the primary one, so two requests together can
        surface listings whose price or distance the primary parameters exclude.
        Everything else stays identical.
        """
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
        if radius_km is not None:
            params["radiusKM"] = str(radius_km)
        if self.min_price is not None:
            params["minPrice"] = str(self.min_price)
        if self.max_price is not None:
            params["maxPrice"] = str(self.max_price)
        if drop_max_price:
            # Must come *after* the ceiling is written, or it is re-added.
            params.pop("maxPrice", None)
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
    notifications_probe_every: int = 2
    # Freshness gate: skip the alert when a listing's age is older than this
    # many minutes. 0 disables the gate.
    max_listed_minutes: int = 8
    # A second, slightly different search merged into every poll.
    second_search: bool = False
    second_search_radius_km: int | None = None
    second_search_drop_max_price: bool = True
    # Alert straight from the notifications page, before the search shows it.
    alert_from_probe: bool = False

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
