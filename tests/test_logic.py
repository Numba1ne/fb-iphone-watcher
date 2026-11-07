"""Tests for the browser-free logic: URL building, filtering, dedupe.

Run with:  python -m pytest tests/ -q     (or: python tests/test_logic.py)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import filters  # noqa: E402
from watcher.marketplace import _parse_location, _parse_title  # noqa: E402
from watcher.models import Listing, SearchSpec, age_minutes  # noqa: E402
from watcher.store import Store  # noqa: E402

EXCLUDE = (
    "for parts",
    "cracked",
    "broken",
    "faulty",
    "icloud locked",
    "locked",
    "blacklisted",
    "sold",
)


def listing(title: str, price: int | None = 300) -> Listing:
    return Listing.from_card("123", title, price, "Croydon")


# --------------------------------------------------------------------- URLs


def test_url_contains_all_filters():
    spec = SearchSpec(
        city_slug="london",
        query="iphone",
        min_price=100,
        max_price=1200,
        radius_km=25,
        latitude=51.5074,
        longitude=-0.1278,
    )
    url = spec.build_url()
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    assert parsed.path == "/marketplace/london/search/"
    assert qs["query"] == ["iphone"]
    assert qs["minPrice"] == ["100"]
    assert qs["maxPrice"] == ["1200"]
    assert qs["sortBy"] == ["creation_time_descend"]
    assert qs["radiusKM"] == ["25"]
    assert qs["lat"] == ["51.5074"]
    # Without this Facebook ignores the sort and serves ~15-hour-old listings.
    assert qs["daysSinceListed"] == ["1"]


def test_url_omits_recency_window_when_disabled():
    spec = SearchSpec("london", "iphone", None, None, 25, 0.0, 0.0, days_since_listed=None)
    qs = parse_qs(urlparse(spec.build_url()).query)
    assert "daysSinceListed" not in qs


def test_url_override_wins():
    spec = SearchSpec(
        city_slug="london",
        query="iphone",
        min_price=None,
        max_price=None,
        radius_km=25,
        latitude=0.0,
        longitude=0.0,
        override_url="https://www.facebook.com/marketplace/london/search/?query=test",
    )
    assert spec.build_url().endswith("query=test")


def test_url_omits_unset_price_bounds():
    spec = SearchSpec("london", "iphone", None, 500, 25, 0.0, 0.0)
    qs = parse_qs(urlparse(spec.build_url()).query)
    assert "minPrice" not in qs
    assert qs["maxPrice"] == ["500"]


# ------------------------------------------------------------------ filters


def kw(title: str) -> bool:
    return filters.matches(listing(title), require_any=("iphone",), exclude=EXCLUDE)


def test_accepts_clean_listing():
    assert kw("iPhone 13 128GB Blue")


def test_accepts_unlocked_iphone():
    # The regression that matters most: "unlocked" must not trip "locked".
    assert kw("Apple iPhone 12 64GB Unlocked")
    assert kw("iPhone 14 Pro - fully UNLOCKED, great condition")


def test_rejects_locked():
    assert not kw("iPhone 13 iCloud locked")
    assert not kw("iPhone 13 activation locked")
    assert not kw("iPhone 11 - locked to EE")


def test_rejects_broken_listings():
    for bad in (
        "iPhone 13 for parts",
        "iPhone 12 cracked screen",
        "iPhone 11 broken",
        "iPhone 13 faulty",
        "iPhone 14 blacklisted",
        "iPhone 13 — sold",
    ):
        assert not kw(bad), bad


def test_rejects_non_iphone():
    assert not kw("Samsung Galaxy S23")
    assert not filters.matches(listing("Samsung Galaxy"), require_any=("iphone",), exclude=())


def test_rejects_zero_price():
    assert not filters.matches(
        listing("iPhone 13", price=0), require_any=("iphone",), exclude=()
    )


def test_price_band_applied_locally():
    items = [listing("iPhone 13", 50), listing("iPhone 13", 500), listing("iPhone 13", 2000)]
    kept = filters.apply(items, require_any=("iphone",), exclude=(), min_price=100, max_price=1200)
    assert [x.price for x in kept] == [500]


def test_unpriced_dropped_when_band_set():
    items = [listing("iPhone 13", None)]
    assert filters.apply(items, require_any=("iphone",), exclude=(), min_price=100) == []
    assert len(filters.apply(items, require_any=("iphone",), exclude=())) == 1


def test_substring_price_words_not_matched():
    # "cracked" shouldn't fire on "crackedless", and word boundaries hold.
    assert filters.matches(
        listing("iPhone 13 with case"), require_any=("iphone",), exclude=("crack",)
    )


# ------------------------------------------------------------------ parsing


def test_title_prefers_image_alt():
    assert _parse_title(["£280", "London", "iPhone 13 128GB"], "iPhone 13 128GB Blue") == (
        "iPhone 13 128GB Blue"
    )


def test_title_skips_placeholder_alt():
    lines = ["£280", "London, GB", "iPhone 13 Pro Max 256GB"]
    assert _parse_title(lines, "Photo of item") == "iPhone 13 Pro Max 256GB"


def test_location_is_line_after_price():
    assert _parse_location(["iPhone 13", "£280", "Croydon, London"]) == "Croydon, London"


def test_location_missing_returns_empty():
    assert _parse_location(["£280"]) == ""


def test_location_is_last_line_not_the_title():
    # Real card order: badge, price, title, location. Taking the first line
    # after the price returned the title, so every listing looked located in
    # its own name.
    lines = [
        "Just listed",
        "£530",
        "iPhone 16 Pro Max - Excellent Condition",
        "Erith, Greenwich, United Kingdom",
    ]
    assert _parse_location(lines) == "Erith, Greenwich, United Kingdom"


def test_title_drops_redundant_location_suffix():
    from watcher.marketplace import _strip_location_suffix

    assert (
        _strip_location_suffix("iPhone 16 Pro in Brighton and Hove", "Brighton and Hove")
        == "iPhone 16 Pro"
    )
    assert _strip_location_suffix("iPhone 13", "London") == "iPhone 13"


# -------------------------------------------------------------------- store


def test_quoted_false_disables_a_channel():
    # bool("false") is True, which would silently enable a disabled channel.
    from watcher.config import _as_bool

    assert _as_bool("false", True) is False
    assert _as_bool("no", True) is False
    assert _as_bool("true", False) is True
    assert _as_bool(None, True) is True
    assert _as_bool(False, True) is False


def test_store_claim_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        with Store(str(Path(tmp) / "t.db")) as store:
            assert store.is_empty
            first = store.claim(listing("iPhone 13"), now="2026-01-01T00:00:00", alert=True)
            second = store.claim(listing("iPhone 13"), now="2026-01-01T00:01:00", alert=True)
            assert first is True
            assert second is False
            assert store.count() == 1


def test_store_survives_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "t.db")
        with Store(path) as store:
            store.claim(listing("iPhone 13"), now="2026-01-01T00:00:00", alert=False)
        with Store(path) as store:
            assert store.count() == 1
            assert not store.is_empty


# ---------------------------------------------------------------- freshness


def test_age_minutes_just_now():
    assert age_minutes("just now") == 0


def test_age_minutes_units():
    assert age_minutes("just listed") == 0
    assert age_minutes("1 minute ago") == 1
    assert age_minutes("5 minutes ago") == 5
    assert age_minutes("23 minutes ago") == 23
    assert age_minutes("2 hours ago") == 120
    assert age_minutes("8 hours ago") == 480
    assert age_minutes("1 day ago") == 24 * 60
    # Facebook sometimes includes the "Listed" prefix in the raw text.
    assert age_minutes("Listed 8 hours ago") == 480


def test_age_minutes_unknown_when_coarse():
    # "today" is too coarse to gate on, so it must not read as zero.
    assert age_minutes("today") is None
    assert age_minutes("") is None


def test_secondary_url_changes_radius_and_drops_ceiling():
    spec = SearchSpec("london", "iphone", 100, 1200, 25, 51.5, -0.12)
    qs = parse_qs(urlparse(spec.build_url(radius_km=40, drop_max_price=True)).query)
    assert qs["radiusKM"] == ["40"]
    assert "maxPrice" not in qs
    assert qs["minPrice"] == ["100"]


def test_secondary_url_matches_primary_when_unchanged():
    spec = SearchSpec("london", "iphone", 100, 1200, 25, 51.5, -0.12)
    assert spec.build_url() == spec.build_url(radius_km=25, drop_max_price=False)


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
