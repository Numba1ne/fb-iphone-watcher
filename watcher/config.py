"""Load and validate config.yaml plus secrets from .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .models import AppConfig, SearchSpec

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised with a human-readable message when config is unusable."""


def _get(section: dict[str, Any], key: str, default: Any) -> Any:
    value = section.get(key, default)
    return default if value is None else value


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

    min_price = _as_int(search.get("min_price"), "search.min_price")
    max_price = _as_int(search.get("max_price"), "search.max_price")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ConfigError(
            f"search.min_price (£{min_price}) is above search.max_price (£{max_price})."
        )

    interval = float(_get(polling, "interval_seconds", 45))
    if interval < 15:
        raise ConfigError(
            f"polling.interval_seconds is {interval:.0f}s. Under 15s is a fast way to "
            f"get a temporary Facebook block — use 45s unless you have a reason."
        )

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    want_telegram = bool(_get(notifications, "telegram", True))
    want_discord = bool(_get(notifications, "discord", True))

    if want_telegram and not (telegram_token and telegram_chat_id):
        raise ConfigError(
            "notifications.telegram is enabled but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID are missing from .env.\n"
            "Either fill them in, or set notifications.telegram: false."
        )
    if want_discord and not discord_url:
        raise ConfigError(
            "notifications.discord is enabled but DISCORD_WEBHOOK_URL is missing "
            "from .env.\nEither fill it in, or set notifications.discord: false."
        )
    if not want_telegram and not want_discord:
        raise ConfigError(
            "Both notification channels are disabled — you would never be told "
            "about a listing. Enable at least one."
        )

    spec = SearchSpec(
        city_slug=str(_get(search, "city_slug", "london")).strip("/").lower(),
        query=str(_get(search, "query", "iphone")),
        min_price=min_price,
        max_price=max_price,
        radius_km=int(_get(search, "radius_km", 25)),
        latitude=float(_get(search, "latitude", 51.5074)),
        longitude=float(_get(search, "longitude", -0.1278)),
        override_url=str(_get(search, "search_url_override", "")).strip(),
    )

    # Relative paths resolve against the project root so the bot behaves the
    # same whether launched by hand or by Task Scheduler.
    def resolve(value: str) -> str:
        p = Path(value)
        return str(p if p.is_absolute() else ROOT / p)

    return AppConfig(
        search=spec,
        interval_seconds=interval,
        jitter_seconds=float(_get(polling, "jitter_seconds", 8)),
        max_cards=int(_get(polling, "max_cards", 30)),
        restart_browser_every=int(_get(polling, "restart_browser_every", 100)),
        headless=bool(_get(browser, "headless", True)),
        user_data_dir=resolve(str(_get(browser, "user_data_dir", "data/browser-profile"))),
        navigation_timeout_ms=int(_get(browser, "navigation_timeout_ms", 30000)),
        require_any=tuple(filters.get("require_any") or ()),
        exclude=tuple(filters.get("exclude") or ()),
        telegram=want_telegram,
        discord=want_discord,
        first_run_silent=bool(_get(notifications, "first_run_silent", True)),
        db_path=resolve(str(_get(paths, "db", "data/bot.db"))),
        log_path=resolve(str(_get(paths, "log", "data/logs/bot.log"))),
        debug_dir=resolve(str(_get(paths, "debug", "data/debug"))),
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        discord_webhook_url=discord_url,
    )
