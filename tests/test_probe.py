"""Notification probe behaviour, with Playwright stubbed out.

The probe is instrumentation riding along with the poll, so the guarantees it
owes the loop are narrow but easy to get wrong: never raise, never report a
failed read as a quiet account, and never lose a page whose list has already
rendered just because the navigation event was slow.

Run:  python -m tests.test_probe
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watcher.notifications as notif_mod  # noqa: E402
from watcher.notifications import (  # noqa: E402
    NotificationLog,
    _open_notifications,
    _parse_age,
    fetch_alert_items,
)

failures: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


class FakePage:
    """A page whose evaluate() answers from a scripted list of results."""

    def __init__(
        self,
        *,
        goto_error: Exception | None = None,
        evaluate_results: list[object] | None = None,
        evaluate_error: Exception | None = None,
    ) -> None:
        self.goto_error = goto_error
        self.evaluate_results = list(evaluate_results or [])
        self.evaluate_error = evaluate_error
        self.evaluate_calls = 0
        self.waits = 0
        self.closed = False
        self.goto_kwargs: dict = {}

    async def goto(self, url: str, **kwargs: object) -> None:
        self.goto_kwargs = kwargs
        if self.goto_error is not None:
            raise self.goto_error

    async def evaluate(self, script: str) -> object:
        self.evaluate_calls += 1
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if not self.evaluate_results:
            return []
        # Hold the last result, so the readiness loop keeps seeing it.
        return self.evaluate_results.pop(0) if len(self.evaluate_results) > 1 else (
            self.evaluate_results[0]
        )

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1

    async def close(self) -> None:
        self.closed = True

    async def query_selector(self, selector: str) -> None:
        return None

    @property
    def url(self) -> str:
        return notif_mod.NOTIFICATIONS_URL


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page

    async def assert_logged_in(self, page: FakePage) -> None:
        return None


def _timeout() -> Exception:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    return PlaywrightTimeoutError("Timeout 30000ms exceeded.")


async def main() -> int:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    print("\nNotification probe\n")

    # 1. A navigation that never fires domcontentloaded must not discard a page
    #    whose rows are already readable. This is the bug that made every probe
    #    report "0 listing(s) visible".
    page = FakePage(
        goto_error=_timeout(),
        evaluate_results=[{"rows": 0, "items": 0}, {"rows": 12, "items": 0}],
    )
    counts = await _open_notifications(page)
    check("a navigation timeout is not fatal", counts.get("rows") == 12)
    check(
        "the probe waits for commit, not domcontentloaded",
        page.goto_kwargs.get("wait_until") == "commit",
    )

    # 2. A blank page is not waited on forever: the readiness poll is bounded.
    #    The fake page returns instantly, so time is faked too — otherwise the
    #    loop would spin on the real clock and this check would mean nothing.
    page = FakePage(evaluate_results=[{"rows": 0, "items": 0}])
    clock = {"now": 0.0}

    def _tick() -> float:
        clock["now"] += notif_mod._READY_POLL_MS / 1000
        return clock["now"]

    with patch.object(notif_mod.time, "monotonic", _tick):
        counts = await _open_notifications(page)
    limit = int(notif_mod._READY_TIMEOUT_S * 1000 / notif_mod._READY_POLL_MS)
    check("a page that never renders returns zero counts", counts.get("rows") == 0)
    check("the readiness wait is bounded", page.waits <= limit + 1)

    # 3. Rendering is detected as soon as listing anchors appear, not at the end.
    page = FakePage(
        evaluate_results=[
            {"rows": 0, "items": 0},
            {"rows": 3, "items": 2},
            {"rows": 9, "items": 9},
        ]
    )
    counts = await _open_notifications(page)
    check("readiness stops at the first rendered list", counts.get("items") == 2)

    # 4. A page that cannot be evaluated at all still returns, rather than raising.
    page = FakePage(evaluate_error=RuntimeError("Target closed"))
    counts = await _open_notifications(page)
    check("an unevaluatable page returns empty counts", counts == {})

    # 5. fetch_alert_items swallows every failure: the poll loop must not die
    #    over a measurement.
    page = FakePage(goto_error=RuntimeError("boom"))
    items = await fetch_alert_items(FakeSession(page))
    check("a failed probe yields no items instead of raising", items == [])
    check("a failed probe still closes its page", page.closed)

    # 6. A page with rows but no Marketplace listings is a distinct outcome, and
    #    says so in the log rather than looking like a failed read.
    page = FakePage(evaluate_results=[{"rows": 0, "items": 0}, {"rows": 12, "items": 0}])

    async def fake_open(p: FakePage) -> dict:
        return {"rows": 12, "items": 0}

    async def fake_read(p: FakePage) -> list[dict]:
        return []

    messages: list[str] = []

    import logging

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("watcher.notifications")
    handler = _Sink(level=logging.INFO)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with patch.object(notif_mod, "_open_notifications", fake_open), patch.object(
            notif_mod, "_read_rows", fake_read
        ):
            items = await fetch_alert_items(FakeSession(page))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)

    check("rendered-but-alertless returns no items", items == [])
    check(
        "rendered-but-alertless is logged as an empty account, not a failure",
        any("none of them a Marketplace saved-search alert" in m for m in messages),
    )

    # 7. A page that renders nothing at all is reported as the other case.
    messages.clear()

    async def fake_open_blank(p: FakePage) -> dict:
        return {"rows": 0, "items": 0}

    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with patch.object(notif_mod, "_open_notifications", fake_open_blank), patch.object(
            notif_mod, "_read_rows", fake_read
        ):
            await fetch_alert_items(FakeSession(page))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
    check(
        "a blank page is reported as an unread page",
        any("rendered no rows at all" in m for m in messages),
    )

    # 8. Row ages parse the way the notification list writes them.
    check("an age is taken from the row text", _parse_age("James sent you a request 2d") == "2d")
    check("the last age in a row wins", _parse_age("5m 2h") == "2h")
    check("text with no age yields nothing", _parse_age("Notifications") == "")
    check("a bare unit is not an age", _parse_age("3 s ") == "3s")

    # 9. The log records the first sighting only, and survives a restart.
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "notif-log.jsonl"
        log = NotificationLog(str(path))
        first = log.record([{"id": "1", "text": "a", "age_text": "5m"}])
        second = log.record([{"id": "1", "text": "a", "age_text": "5m"}])
        check("the first sighting is recorded", len(first) == 1)
        check("a repeat sighting is not re-recorded", second == [])

        reloaded = NotificationLog(str(path))
        check("a restart does not re-record a known id", reloaded.record(
            [{"id": "1", "text": "a", "age_text": "5m"}]
        ) == [])

        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        check("one line per listing", len(rows) == 1)
        check("the entry carries an absolute timestamp", "first_seen" in rows[0])

    # 10. A corrupt log line is skipped rather than taking the log down.
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "notif-log.jsonl"
        path.write_text('{"id": "9"}\nnot json\n\n', encoding="utf-8")
        log = NotificationLog(str(path))
        check("a corrupt log line is skipped", log._seen == {"9"})

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all probe checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
