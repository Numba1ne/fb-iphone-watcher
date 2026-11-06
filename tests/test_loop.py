"""WatcherRunner behaviour with Facebook stubbed out.

Confirms the guarantees that matter most: the first run stays silent, a second
poll doesn't re-alert, filtered-out listings never reach the store, failures are
recorded rather than fatal, and start/stop leaves an honest state behind.

Run:  python -m tests.test_loop
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watcher.runner as runner_mod  # noqa: E402
from tests.test_notify import base_config  # noqa: E402
from watcher.models import Listing  # noqa: E402
from watcher.runner import WatcherRunner  # noqa: E402
from watcher.store import Store  # noqa: E402

CARD_LIST = [
    Listing.from_card("1", "iPhone 13 128GB", 280, "Croydon, London"),
    Listing.from_card("2", "iPhone 14 for parts", 150, "Epsom"),          # filtered
    Listing.from_card("3", "iPhone 12, iCloud locked", 200, "Sutton"),     # filtered
    Listing.from_card("4", "Samsung Galaxy S22", 400, "London"),           # filtered
    Listing.from_card("5", "iPhone 15 256GB", 5000, "London"),             # over band
]

# The keyword list lives in config.yaml, not in code; supply it explicitly so
# this suite doesn't depend on whether a real config.yaml exists.
TEST_EXCLUDE = (
    "for parts", "parts only", "spares", "cracked", "broken", "faulty",
    "not working", "screen damage", "icloud locked", "activation lock",
    "locked", "blacklisted", "bad imei", "sold",
)


class FakePage:
    async def close(self) -> None:
        return None


class FakeSession:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def restart(self) -> None:
        return None


def make_cfg(tmp: Path, **overrides: object):
    cfg = replace(
        base_config(telegram=False, discord=False),
        db_path=str(tmp / "bot.db"),
        debug_dir=str(tmp / "debug"),
        first_run_silent=True,
        exclude=TEST_EXCLUDE,
        interval_seconds=45,
    )
    return replace(cfg, **overrides) if overrides else cfg


def _patches(cards, sent):
    async def fake_open(session, cfg):
        return FakePage()

    async def fake_parse(page, limit):
        # Fresh objects each poll, like a real scrape.
        return [Listing.from_card(c.id, c.title, c.price, c.location) for c in cards], 0

    from watcher.notify import Notifier

    async def capture(self, listing):
        sent.append(listing)
        return True

    return (
        patch.object(runner_mod, "BrowserSession", lambda cfg: FakeSession()),
        patch.object(runner_mod, "open_search", fake_open),
        patch.object(runner_mod, "parse_cards", fake_parse),
        patch.object(Notifier, "send", capture),
    )


async def run_polls(cfg, cards, *, polls=1):
    """Poll `polls` times with scraping replaced by `cards`, then return state.

    Uses poll_once (synchronous, deterministic) rather than the background
    loop, so results don't depend on task scheduling.
    """
    sent: list[Listing] = []
    runner = WatcherRunner(cfg)
    p1, p2, p3, p4 = _patches(cards, sent)
    with p1, p2, p3, p4:
        for _ in range(polls):
            await runner.poll_once()

    with Store(cfg.db_path) as store:
        rows = store.count()
    return sent, runner.status, rows


async def run_loop_mode(cfg, cards):
    """Exercise the background loop specifically, since that's the dashboard path."""
    sent: list[Listing] = []
    runner = WatcherRunner(cfg)
    p1, p2, p3, p4 = _patches(cards, sent)
    with p1, p2, p3, p4:
        await runner.start()
        await _wait_for_polls(runner, 1)
        await runner.stop()
    return sent, runner.status


async def _wait_for_polls(runner, target: int, timeout: float = 10) -> None:
    """Wait for the runner's cumulative poll count to reach `target`.

    Callers that stop and restart must capture the post-stop count first;
    stop() deliberately leaves the counter intact.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while runner.status.poll_count < target:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"poll did not complete: count={runner.status.poll_count} target={target} "
                f"state={runner.status.state} error={runner.status.last_error}"
            )
        await asyncio.sleep(0.02)


async def main() -> int:
    failures = []

    def check(label: str, condition: bool) -> None:
        print(f"  {'PASS ' if condition else 'FAIL '} {label}")
        if not condition:
            failures.append(label)

    # 1. First run: records matching listings, sends nothing.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))
        sent, status, rows = await run_polls(cfg, CARD_LIST)
        check("first run sends no notifications", sent == [])
        check(f"first run stores only matches (rows={rows})", rows == 1)
        check("poll recorded in history", status.poll_count == 1)
        check("runner ends stopped after stop()", status.state == "stopped")

    # 2. Repeat poll: nothing new.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))
        await run_polls(cfg, CARD_LIST)
        sent, status, rows = await run_polls(cfg, CARD_LIST, polls=2)
        check("repeat polls alert nothing", sent == [])
        check(f"repeat polls don't duplicate rows (rows={rows})", rows == 1)
        check("poll count accumulates", status.poll_count == 2)

    # 3. A new listing alerts exactly once.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))
        await run_polls(cfg, CARD_LIST)
        extra = Listing.from_card("99", "iPhone 13 Pro 256GB", 320, "Bromley")
        sent, _, _ = await run_polls(cfg, CARD_LIST + [extra])
        check("new listing alerted", [x.id for x in sent] == ["99"])

    # 4. first_run_silent: false alerts on the backlog.
    with tempfile.TemporaryDirectory() as tmpdir:
        sent, _, _ = await run_polls(make_cfg(Path(tmpdir), first_run_silent=False), CARD_LIST)
        check("first_run_silent=false alerts on backlog", [x.id for x in sent] == ["1"])

    # 5. A failing poll is recorded and the runner keeps going.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))

        async def broken_open(session, cfg):
            raise RuntimeError("network down")

        runner = WatcherRunner(cfg)
        with patch.object(runner_mod, "BrowserSession", lambda cfg: FakeSession()), patch.object(
            runner_mod, "open_search", broken_open
        ):
            await runner.start()
            await _wait_for_polls(runner, 1)
            await runner.stop()
        check("failed poll recorded, runner survives", runner.status.consecutive_failures == 1)
        check("failure surfaces in status", "network down" in (runner.status.last_error or ""))
        with Store(cfg.db_path) as store:
            ok = store.recent_polls(1)[0]["ok"]
        check("failed poll stored with ok=0", ok == 0)

    # 6. Stopping an idle runner is a no-op, not an error.
    with tempfile.TemporaryDirectory() as tmpdir:
        runner = WatcherRunner(make_cfg(Path(tmpdir)))
        check("stop() on a stopped runner returns False", await runner.stop() is False)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
