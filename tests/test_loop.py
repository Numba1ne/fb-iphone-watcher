"""WatcherRunner behaviour with Facebook stubbed out.

Confirms the guarantees that matter most: the first run stays silent, a second
poll doesn't re-alert, filtered-out listings never reach the store, failures are
recorded rather than fatal, and start/stop leaves an honest state behind.

Run:  python -m tests.test_loop
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watcher.marketplace as marketplace_mod  # noqa: E402
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


@contextmanager
def _capture_runner_log():
    """Collect the messages watcher.runner emits at INFO and above."""
    messages: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Sink(level=logging.INFO)
    logger = logging.getLogger("watcher.runner")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


class FakePage:
    """Stands in for a search page; its content is never read."""

    async def content(self) -> str:
        return "<html></html>"

    async def goto(self, *args: object, **kwargs: object) -> None:
        return None

    async def wait_for_timeout(self, *args: object, **kwargs: object) -> None:
        return None

    async def inner_text(self, *args: object, **kwargs: object) -> str:
        return ""

    async def close(self) -> None:
        return None


class FakeSession:
    restarts = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def restart(self) -> None:
        FakeSession.restarts += 1

    async def new_page(self) -> FakePage:
        return FakePage()


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


def _ages_for(cards, ages, now):
    """Turn a test's shorthand into the page's id -> epoch map.

    `ages` is either a dict of id -> minutes, or a `minutes()` call producing
    the seconds applied to every card, or None for "the page carried no
    timestamps at all" — the case that must read as an unknown age rather than
    as brand new.
    """
    if ages is None:
        return {}
    if isinstance(ages, dict):
        return {
            cid: now - int(minutes) * 60
            for cid, minutes in ages.items()
            if any(c.id == cid for c in cards)
        }
    if callable(ages):
        ages = ages()
    # Seconds as well as minutes: a test needs to land a stamp just short of
    # an age boundary to see which side the gate rounds to.
    return {c.id: now - int(ages) for c in cards}


def minutes(count: float) -> int:
    """A card `count` minutes old."""
    return int(count * 60)


def _patches(cards, sent, ages=minutes(2), *, server_skew=0, render_lag=0.0):
    """Patch the scrape away."""
    async def fake_open(session, cfg, url=None):
        # `render_lag` is how many seconds the page takes to arrive, which
        # the gate counts as age: a listing is older by that much.
        if render_lag:
            await asyncio.sleep(render_lag)
        return FakePage()

    async def fake_parse(page, limit):
        # Fresh objects each poll, like a real scrape.
        return [Listing.from_card(c.id, c.title, c.price, c.location) for c in cards], 0

    async def fake_times(page):
        # `server_skew` is how far Facebook's clock runs from this machine's.
        server = int(time.time()) + server_skew
        posted = _ages_for(cards, ages, server)
        return posted, (server if posted else None)

    from watcher.notify import Notifier

    async def capture(self, listing):
        sent.append(listing)
        return True

    return (
        patch.object(runner_mod, "BrowserSession", lambda cfg: FakeSession()),
        patch.object(runner_mod, "open_search", fake_open),
        patch.object(runner_mod, "parse_cards", fake_parse),
        # The page's own timestamps are the only source of an age now, so this
        # is where a test says how old its cards are. Patched in the
        # marketplace module too: the runner imports it by name, so the stub
        # above can be shadowed by whichever reference is live at call time.
        patch.object(marketplace_mod, "read_creation_times", fake_times),
        patch.object(runner_mod, "read_creation_times", fake_times),
        patch.object(Notifier, "send", capture),
    )


async def run_polls(cfg, cards, *, polls=1, ages=minutes(2)):
    """Poll `polls` times with scraping replaced by `cards`, then return state.

    Uses poll_once (synchronous, deterministic) rather than the background
    loop, so results don't depend on task scheduling.
    """
    sent: list[Listing] = []
    runner = WatcherRunner(cfg)
    p1, p2, p3, p4, p5, p6 = _patches(cards, sent, ages)
    with p1, p2, p3, p4, p5, p6:
        for _ in range(polls):
            await runner.poll_once()

    with Store(cfg.db_path) as store:
        rows = store.count()
    return sent, runner.status, rows


async def run_loop_mode(cfg, cards):
    """Exercise the background loop specifically, since that's the dashboard path."""
    sent: list[Listing] = []
    runner = WatcherRunner(cfg)
    p1, p2, p3, p4, p5, p6 = _patches(cards, sent)
    with p1, p2, p3, p4, p5, p6:
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


async def _warmup_run(age: int | None, *, max_listed_minutes: int = 8):
    """Run a fresh process's first poll with its card this many seconds old.

    `age=None` means the page carried no timestamp, so the age is unknowable.
    Returns what was sent, the log messages, and the stored row.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), max_listed_minutes=max_listed_minutes)
        sent: list[Listing] = []
        cards = [CARD_LIST[0]]
        runner = WatcherRunner(cfg)
        p1, p2, p3, p4, p5, p6 = _patches(cards, sent, age)

        with p1, p2, p3, p4, p5, p6:
            with _capture_runner_log() as logs:
                await runner.poll_once()
        with Store(cfg.db_path) as store:
            row = store.search_listings(limit=1)[0][0]
        return sent, logs, row


async def main() -> int:
    failures = []

    def check(label: str, condition: bool) -> None:
        print(f"  {'PASS ' if condition else 'FAIL '} {label}")
        if not condition:
            failures.append(label)

    # 1. First run over an existing backlog: records matching listings, sends
    # nothing. The cards are given ages older than the gate, because a warm-up
    # listing *younger* than it is announced on purpose (see case 12).
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))
        sent, status, rows = await run_polls(cfg, CARD_LIST, ages=minutes(60))
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
        sent: list[Listing] = []
        cards = list(CARD_LIST)
        runner = WatcherRunner(cfg)
        # The backlog is old; the listing that appears mid-run is new.
        ages = {c.id: 60 for c in CARD_LIST}
        ages["99"] = 2
        p1, p2, p3, p4, p5, p6 = _patches(cards, sent, ages)
        extra = Listing.from_card("99", "iPhone 13 Pro 256GB", 320, "Bromley")
        with p1, p2, p3, p4, p5, p6:
            await runner.poll_once()   # warm-up: records the backlog silently
            cards.append(extra)        # a listing posted while we were running
            await runner.poll_once()
        check("new listing alerted", [x.id for x in sent] == ["99"])

    # 4. first_run_silent: false alerts on the backlog.
    with tempfile.TemporaryDirectory() as tmpdir:
        sent, _, _ = await run_polls(make_cfg(Path(tmpdir), first_run_silent=False), CARD_LIST)
        check("first_run_silent=false alerts on backlog", [x.id for x in sent] == ["1"])

    # 5. A failing poll is recorded and the runner keeps going.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))

        async def broken_open(session, cfg, url=None):
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

    # 7. restart_browser_every actually restarts the browser on schedule.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), restart_browser_every=1)
        sent: list[Listing] = []
        runner = WatcherRunner(cfg)
        FakeSession.restarts = 0
        p1, p2, p3, p4, p5, p6 = _patches(CARD_LIST, sent)
        with p1, p2, p3, p4, p5, p6:
            await runner.start()
            await _wait_for_polls(runner, 1)
            runner.trigger_poll()  # skip the interval so the test stays fast
            await _wait_for_polls(runner, 2)
            await runner.stop()
        check(
            f"browser restarted on schedule (restarts={FakeSession.restarts})",
            FakeSession.restarts >= 1,
        )

    # 8. Freshness gate: a listing the page's timestamp says is old is recorded
    # but never alerted, and its row stays notified=0.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), max_listed_minutes=8, first_run_silent=False)
        sent: list[Listing] = []
        runner = WatcherRunner(cfg)
        p1, p2, p3, p4, p5, p6 = _patches(CARD_LIST, sent, ages=minutes(20))

        with p1, p2, p3, p4, p5, p6:
            with _capture_runner_log() as logs:
                await runner.poll_once()
        check("stale listing not alerted", sent == [])
        with Store(cfg.db_path) as store:
            rows = store.count()
            row = store.search_listings(limit=1)[0][0]
        check("stale listing still recorded", rows == 1)
        check("stale listing marked not alerted", row["notified"] == 0)
        check(
            "stale skip is logged with its age",
            any(m.startswith("SKIPPED (too old: 20 min)") for m in logs),
        )

    # 9. The gate lets a fresh listing through.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), max_listed_minutes=8, first_run_silent=False)
        sent: list[Listing] = []
        runner = WatcherRunner(cfg)
        p1, p2, p3, p4, p5, p6 = _patches(CARD_LIST, sent, ages=minutes(3))

        with p1, p2, p3, p4, p5, p6:
            with _capture_runner_log() as logs:
                await runner.poll_once()
        check("fresh listing alerted", [x.id for x in sent] == ["1"])
        with Store(cfg.db_path) as store:
            row = store.search_listings(limit=1)[0][0]
        check("the delivered alert marks the row as told", row["notified"] == 1)
        check(
            "alert is logged with the listing's age",
            any(m.startswith("ALERT (age: 3 min)") for m in logs),
        )

    # 10. Restarting over yesterday's database announces nothing. Every start
    # records what is already on the page, so coming back after a night off
    # doesn't replay the backlog.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir))
        await run_polls(cfg, CARD_LIST)                     # first process
        sent, _, rows = await run_polls(cfg, CARD_LIST)     # restarted process
        check("restart does not replay the backlog", sent == [])
        check(f"restart still records the backlog (rows={rows})", rows == 1)

    # 11. A missing timestamp must never read as fresh. It used to fall back to
    # the card's "Just listed" badge, which scored every failed lookup as 0
    # minutes — the bug that announced 14 stale listings at once.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), max_listed_minutes=8, first_run_silent=False)
        sent: list[Listing] = []
        runner = WatcherRunner(cfg)
        p1, p2, p3, p4, p5, p6 = _patches(CARD_LIST, sent, ages=None)

        with p1, p2, p3, p4, p5, p6:
            with _capture_runner_log() as logs:
                await runner.poll_once()
        check(
            "unreadable age is logged as unknown, never as fresh",
            any(m.startswith("ALERT (age: unknown)") for m in logs)
            and not any(m.startswith("ALERT (age: 0 min)") for m in logs),
        )
        check("unreadable age still alerts rather than silently dropping", len(sent) == 1)

    # 12. The warm-up is age-checked: a listing whose timestamp proves it was
    # posted while the bot was down still gets announced, while yesterday's
    # backlog and unreadable ages stay quiet.
    sent, logs, row = await _warmup_run(minutes(3))
    check("warm-up announces a listing posted while stopped", [x.id for x in sent] == ["1"])
    check(
        "the warm-up alert says why it was announced",
        any("posted while stopped" in m for m in logs),
    )
    check("the announced warm-up row is marked as told", row["notified"] == 1)

    sent, _, row = await _warmup_run(minutes(18))
    check("warm-up stays quiet on yesterday's listing", sent == [])
    check("yesterday's listing is stored as untold", row["notified"] == 0)

    sent, logs, _ = await _warmup_run(None)
    check("warm-up stays quiet when the age can't be read", sent == [])
    check(
        "the unreadable warm-up listing logs as recorded",
        any(m.startswith("RECORDED (startup)") for m in logs),
    )

    sent, _, _ = await _warmup_run(minutes(3), max_listed_minutes=0)
    check("warm-up stays quiet when the gate is disabled", sent == [])

    # 13. A delivery that fails is not recorded as an alert. The dashboard's
    # ALERTED chip must mean "delivered", not "attempted".
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), max_listed_minutes=8, first_run_silent=False)
        runner = WatcherRunner(cfg)
        p1, p2, p3, p4, p5, p6 = _patches(CARD_LIST, [], ages=minutes(2))
        from watcher.notify import Notifier

        async def failing_send(self, listing):
            return False

        with p1, p2, p3, p4, p5, p6, patch.object(Notifier, "send", failing_send):
            await runner.poll_once()
        with Store(cfg.db_path) as store:
            row = store.search_listings(limit=1)[0][0]
        check("a failed send leaves the row untold", row["notified"] == 0)
        check("a failed send is not counted as an alert", runner.last_result[1] == 0)

    # 14. Ages are measured against Facebook's clock, not this machine's. The
    # page's `server_time` is the moment it was rendered, so a machine ten
    # minutes behind Facebook must not make 3-minute-old listings look brand
    # new — every alert's age has to be counted from Facebook's clock.
    only_one = CARD_LIST[:1]   # the one card that passes the filters
    for skew, label in ((-600, "behind"), (600, "ahead of")):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(Path(tmpdir), max_listed_minutes=8, first_run_silent=False)
            sent: list[Listing] = []
            runner = WatcherRunner(cfg)
            p1, p2, p3, p4, p5, p6 = _patches(
                only_one, sent, ages=minutes(3), server_skew=skew
            )

            with p1, p2, p3, p4, p5, p6:
                with _capture_runner_log() as logs:
                    await runner.poll_once()
            check(
                f"ages are read on Facebook's clock with this clock {skew // 60:+d} "
                f"min {label}",
                [x.id for x in sent] == ["1"]
                and any(m.startswith("ALERT (age: 3 min)") for m in logs),
            )

    # 15. Time spent between the page being rendered and the listing being
    # judged counts as age. `server_time` dates the render, so a page that took
    # a moment and a half to arrive holds listings that much older than their
    # stamps alone suggest — the age must round up, never down.
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = make_cfg(Path(tmpdir), max_listed_minutes=8, first_run_silent=False)
        sent: list[Listing] = []
        runner = WatcherRunner(cfg)
        # 7 minutes on the stamp plus 1.2s of render is still 7 whole minutes;
        # 59.9 minutes plus 1.2s crosses into the eighth.
        p1, p2, p3, p4, p5, p6 = _patches(
            CARD_LIST[:1], sent, ages=minutes(7) + 59, render_lag=1.2
        )

        with p1, p2, p3, p4, p5, p6:
            with _capture_runner_log() as logs:
                await runner.poll_once()
        check(
            "the render wait is counted, so the age is never understated",
            any(m.startswith("ALERT (age: 8 min)") for m in logs),
        )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
