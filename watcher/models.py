"""Immutable data types passed between modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode


@dataclass(frozen=True)
class Listing:
    """A single Marketplace listing, normalised from a scraped card."""

    id: str
    title: str
    price: int | None
    location: str
    url: str

    @classmethod
    def from_card(
        cls, item_id: str, title: str, price: int | None, location: str
    ) -> "Listing":
        # Rebuild the URL from the id rather than trusting the scraped href, so
        # tracking params and localisation prefixes never leak into the alert.
        return cls(
            id=item_id,
            title=title.strip(),
            price=price,
            location=location.strip() or "Unknown location",
            url=f"https://www.facebook.com/marketplace/item/{item_id}",
        )

    @property
    def price_text(self) -> str:
        return "Price unknown" if self.price is None else f"£{self.price:,}"


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

    @property
    def now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
