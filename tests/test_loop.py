"""End-to-end poll behaviour with Facebook stubbed out.

Confirms the guarantees that matter most: the first run stays silent, a second
poll doesn't re-alert, filtered-out listings never reach the store, and an
unexpected error surfaces rather than being swallowed.

Run:  python tests/test_loop.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app  # noqa: E402
from tests.test_notify import base_config  # noqa: E402
from watcher.models import Listing  # noqa: E402
from watcher.store import Store  # noqa: E402

CARD_LIST = [
    Listing.from_card("1", "iPhone 13 128GB", 280, "Croydon, London"),
    Listing.from_card("2", "iPhone 14 for parts", 150, "Epsom"),          # filtered
    Listing.from_card("3", "iPhone 12, iCloud locked", 200, "Sutton"),     # filtered
    Listing.from_card("4", "Samsung Galaxy S22", 400, "London"),           # filtered
    Listing.from_card("5", "iPhone 15 256GB", 5000, "London"),             # over band
]


# The keyword exclusion list lives in config.yaml, not in code, so the test
# supplies it explicitly — otherwise this suite would pass or fail depending on
# whether a real config.yaml happens to exist. These match config.example.yaml.
TEST_EXCLUDE = (
    "for parts",
    "parts only",
    "spares",
    "cracked",
    "broken",
    "faulty",
    "not working",
    "screen damage",
    "icloud locked",
    "activation lock",
    "locked",
    "blacklisted",
    "bad imei",
    "sold",
)


class FakePage:
    async def close(self) -> None:
        return None


def make_cfg(tmp: Path, **overrides: object):
    cfg = replace(
        base_config(telegram=False, discord=False),
        db_path=str(tmp / "bot.db"),
        first_run_silent=True,
        exclude=TEST_EXCLUDE,
    )
    return replace(cfg, **overrides) if overrides else cfg


async def poll(cfg, cards=None, unparsed=0):
    """Run _poll_once with scraping replaced by the given cards."""
    cards = CARD_LIST if cards is None else cards
    sent: list[Listing] = []

    async def fake_open(session, cfg):
        return FakePage()

    async def fake_parse(page, limit):
        return list(cards), unparsed

    from watcher.notify import Notifier

    original = Notifier.send

    async def capture(self, listing):
        sent.append(listing)
        return True

    with patch.object(app, "open_search", fake_open), patch.object(
        app, "parse_cards", fake_parse
    ), patch.object(Notifier, "send", capture):
        result = await app._poll_once(cfg, session=None, debug=False)
    return result, sent


async def main() -> int:
    failures = []

    def check(label: str, condition: bool) -> None:
        print(f"  {'PASS ' if condition else 'FAIL '} {label}")
        if not condition:
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cfg = make_cfg(tmp)

        # 1. First run: records matching listings, sends nothing.
        (parsed, new), sent = await poll(cfg)
        check(f"first run parses all cards (parsed={parsed})", parsed == 5)
        check(f"first run stores only matches (new={new})", new == 1)
        check("first run sends no notifications", sent == [])
        with Store(cfg.db_path) as store:
            check(f"store holds 1 row (got {store.count()})", store.count() == 1)

        # 2. Same cards again: nothing new, nothing sent.
        (_, new), sent = await poll(cfg)
        check(f"second identical poll yields new={new}", new == 0)
        check("second poll sends nothing", sent == [])

        # 3. A genuinely new listing does alert, exactly once.
        new_listing = Listing.from_card("99", "iPhone 13 Pro 256GB", 320, "Bromley")
        (_, new), sent = await poll(cfg, cards=CARD_LIST + [new_listing])
        check("new listing detected", new == 1)
        check("new listing notified once", len(sent) == 1 and sent[0].id == "99")

        (_, new), sent = await poll(cfg, cards=CARD_LIST + [new_listing])
        check("no duplicate alert on re-poll", new == 0 and sent == [])

    # 4. first_run_silent: false alerts on the backlog instead.
    with tempfile.TemporaryDirectory() as tmpdir2:
        cfg2 = make_cfg(Path(tmpdir2), first_run_silent=False)
        (_, new), sent = await poll(cfg2)
        check("first_run_silent=false alerts on backlog", new == 1 and len(sent) == 1)

    # 5. Scrape errors propagate so the loop can log and continue.
    with tempfile.TemporaryDirectory() as tmpdir3:
        cfg3 = make_cfg(Path(tmpdir3))

        async def broken_open(session, cfg):
            raise RuntimeError("network down")

        with patch.object(app, "open_search", broken_open):
            try:
                await app._poll_once(cfg3, session=None, debug=False)
                check("scrape error propagates", False)
            except RuntimeError:
                check("scrape error propagates", True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
