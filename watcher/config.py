"""Load and validate config.yaml plus secrets from .env."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .models import AppConfig, SearchSpec

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised with a human-readable message when config is unusable."""


def _get(section: dict[str, Any], key: str, default: Any) -> Any:
    value = section.get(key, default)
    return default if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    """Parse a YAML boolean without `bool()`'s trap.

    `bool("false")` is True, so a quoted `telegram: "false"` would silently
    enable a channel the user turned off.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_int(value: Any, label: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a whole number, got {value!r}") from exc


def load(config_path: str | Path | None = None) -> AppConfig:
    """Parse config and environment into a validated AppConfig."""
    config_path = Path(config_path) if config_path else ROOT / "config.yaml"
    load_dotenv(ROOT / ".env")

    if not config_path.exists():
        raise ConfigError(
            f"No config found at {config_path}.\n"
            f"Copy config.example.yaml to config.yaml and edit it."
        )

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc

    search = raw.get("search") or {}
    polling = raw.get("polling") or {}
    browser = raw.get("browser") or {}
    filters = raw.get("filters") or {}
    notifications = raw.get("notifications") or {}
    paths = raw.get("paths") or {}
    freshness = raw.get("freshness") or {}

    min_price = _as_int(search.get("min_price"), "search.min_price")
    max_price = _as_int(search.get("max_price"), "search.max_price")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ConfigError(
            f"search.min_price (£{min_price}) is above search.max_price (£{max_price})."
        )

    interval = float(_get(polling, "interval_seconds", 20))
    if interval < 15:
        raise ConfigError(
            f"polling.interval_seconds is {interval:.0f}s. Under 15s is a fast way to "
            f"get a temporary Facebook block. 20s is the aggressive setting; 30s is "
            f"the safer one."
        )

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    want_telegram = _as_bool(notifications.get("telegram"), True)
    want_discord = _as_bool(notifications.get("discord"), True)

    telegram_ready = bool(telegram_token and telegram_chat_id)
    discord_ready = bool(discord_url)

    # A channel that is switched on but has no credentials is a wiring mistake
    # worth stopping for — it would silently never deliver. A channel that is
    # switched *on* with credentials, or a run with both switched off, is fine:
    # both are useful for checking that scraping and the dashboard work before
    # the alerting is wired up. Deliveries are logged when they have nowhere
    # to go, so silence is never mysterious.
    if want_telegram and not telegram_ready:
        raise ConfigError(
            "notifications.telegram is enabled but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID are missing from .env.\n"
            "Either fill them in, or set notifications.telegram: false."
        )
    if want_discord and not discord_ready:
        raise ConfigError(
            "notifications.discord is enabled but DISCORD_WEBHOOK_URL is missing "
            "from .env.\nEither fill it in, or set notifications.discord: false."
        )
    if not want_telegram and not want_discord:
        log.warning(
            "No notification channel is enabled — listings will be recorded and "
            "shown on the dashboard, but nothing will be sent."
        )

    spec = SearchSpec(
        city_slug=str(_get(search, "city_slug", "london")).strip("/").lower(),
        query=str(_get(search, "query", "iphone")),
        min_price=min_price,
        max_price=max_price,
        radius_km=int(_get(search, "radius_km", 25)),
        latitude=float(_get(search, "latitude", 51.5074)),
        longitude=float(_get(search, "longitude", -0.1278)),
        days_since_listed=_as_int(
            search.get("days_since_listed", 1), "search.days_since_listed"
        ),
        override_url=str(_get(search, "search_url_override", "")).strip(),
    )

    # `notifications.max_age_minutes` is the documented key. The older
    # `freshness.max_listed_minutes` spelling is still honoured so an existing
    # config.yaml keeps working; null or 0 disables the gate either way.
    raw_max_age = notifications.get("max_age_minutes")
    if raw_max_age is None:
        raw_max_age = freshness.get("max_listed_minutes", 0)
    max_listed = _as_int(raw_max_age, "notifications.max_age_minutes") or 0
    if max_listed < 0:
        raise ConfigError("notifications.max_age_minutes must be 0 or more.")

    second_radius = _as_int(
        freshness.get("second_search_radius_km"), "freshness.second_search_radius_km"
    )

    # Relative paths resolve against the project root so the bot behaves the
    # same whether launched by hand or by Task Scheduler.
    def resolve(value: str) -> str:
        p = Path(value)
        return str(p if p.is_absolute() else ROOT / p)

    return AppConfig(
        search=spec,
        interval_seconds=interval,
        jitter_seconds=float(_get(polling, "jitter_seconds", 3)),
        max_cards=int(_get(polling, "max_cards", 30)),
        restart_browser_every=int(_get(polling, "restart_browser_every", 20)),
        headless=_as_bool(browser.get("headless"), True),
        user_data_dir=resolve(str(_get(browser, "user_data_dir", "data/browser-profile"))),
        navigation_timeout_ms=int(_get(browser, "navigation_timeout_ms", 30000)),
        require_any=tuple(filters.get("require_any") or ()),
        exclude=tuple(filters.get("exclude") or ()),
        telegram=want_telegram,
        discord=want_discord,
        first_run_silent=_as_bool(notifications.get("first_run_silent"), True),
        db_path=resolve(str(_get(paths, "db", "data/bot.db"))),
        log_path=resolve(str(_get(paths, "log", "data/logs/bot.log"))),
        debug_dir=resolve(str(_get(paths, "debug", "data/debug"))),
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        discord_webhook_url=discord_url,
        notifications_probe=_as_bool(notifications.get("probe"), False),
        notifications_probe_every=max(
            1, int(_get(notifications, "probe_every", 2))
        ),
        max_listed_minutes=max_listed,
        second_search=_as_bool(freshness.get("second_search"), False),
        second_search_radius_km=second_radius,
        second_search_drop_max_price=_as_bool(
            freshness.get("second_search_drop_max_price"), True
        ),
        alert_from_probe=_as_bool(notifications.get("alert_from_probe"), False),
    )
