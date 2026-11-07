"""Owns the polling loop as a startable/stoppable task.

The loop and the web server run in one process and one event loop, so this
object is the single source of truth for "is it running?". Nothing infers
state from a log line or a PID file, which is how these two get to disagree.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import filters
from .browser import BrowserSession, SessionExpired
from .marketplace import dump_debug, fetch_listed_age, open_search, parse_cards
from .models import AppConfig, Listing
from .notifications import NotificationLog, fetch_alert_items
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Status:
    state: str = "stopped"           # stopped | starting | running | stopping | failed
    started_at: str | None = None
    last_poll_at: str | None = None
    last_error: str | None = None
    poll_count: int = 0
    consecutive_failures: int = 0
    next_poll_in: float | None = None
    session_expired: bool = False

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "poll_count": self.poll_count,
            "consecutive_failures": self.consecutive_failures,
            "next_poll_in": (
                round(max(0.0, self.next_poll_in), 1)
                if self.next_poll_in is not None
                else None
            ),
            "session_expired": self.session_expired,
        }


class WatcherRunner:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.status = Status()
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self._poll_now = False
        self._session: BrowserSession | None = None
        self.last_result: tuple[int, int] = (0, 0)  # (parsed, alerted) from the last attempt
        self._notif_log: NotificationLog | None = None

    # -------------------------------------------------------------- control

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Start the loop. Returns False if it was already running."""
        if self.running:
            return False
        self._stop_requested = False
        self.status = Status(state="starting", started_at=_utc_now())
        self._task = asyncio.create_task(self._run(), name="watcher-loop")
        return True

    async def stop(self, *, timeout: float = 45) -> bool:
        """Ask the loop to stop and wait for it. False if it wasn't running."""
        if not self.running:
            return False
        self._stop_requested = True
        self.status.state = "stopping"
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("Loop did not stop in %.0fs — cancelling", timeout)
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return True

    def trigger_poll(self) -> None:
        """Skip the remaining wait and poll now."""
        self._poll_now = True

    async def poll_once(self) -> None:
        """Run exactly one poll on the caller's event loop.

        Used by the tests and by any caller that wants a single deterministic
        poll, where a background task with its own scheduling would make the
        result depend on timing.
        """
        # Only reset on the first call: repeated poll_once calls should
        # accumulate the count, not start from zero each time.
        if self.status.started_at is None:
            self.status = Status(state="running", started_at=_utc_now())
        else:
            self.status.state = "running"
        started = _utc_now()
        try:
            self._session = BrowserSession(self.cfg)
            await self._session.start()
            await self._poll()
        except SessionExpired as exc:
            self._record_poll(started, ok=False, error=str(exc))
            self.status.state = "failed"
            self.status.session_expired = True
            self.status.last_error = str(exc)
            raise
        except Exception as exc:
            self._record_poll(started, ok=False, error=f"{type(exc).__name__}: {exc}")
            self.status.state = "failed"
            self.status.last_error = str(exc)
            raise
        finally:
            if self._session is not None:
                await self._session.stop()
                self._session = None

        self._record_poll(started, ok=True)
        self.status.state = "stopped"
        self.status.poll_count += 1

    def _record_poll(self, started: str, *, ok: bool, error: str = "") -> None:
        parsed, alerted = self.last_result
        with Store(self.cfg.db_path) as store:
            store.record_poll(
                started_at=started,
                finished_at=_utc_now(),
                parsed=parsed if ok else 0,
                new=alerted if ok else 0,
                ok=ok,
                error=error,
            )

    # ----------------------------------------------------------------- loop

    async def _run(self) -> None:
        cfg = self.cfg
        try:
            self._session = BrowserSession(cfg)
            await self._session.start()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            log.exception("Could not start browser")
            self.status.state = "failed"
            self.status.last_error = f"Browser failed to start: {exc}"
            return

        self.status.state = "running"
        failures = 0
        polls_since_restart = 0

        try:
            while not self._stop_requested:
                wait = cfg.interval_seconds
                started = _utc_now()
                cycle_start = time.monotonic()
                try:
                    await self._poll()
                    self._record_poll(started, ok=True)
                    failures = 0
                    self.status.consecutive_failures = 0
                except SessionExpired as exc:
                    # Not retried: retrying burns requests while Facebook shows
                    # a login wall. The user must re-authenticate.
                    log.error("Session expired: %s", exc)
                    self._record_poll(started, ok=False, error=str(exc))
                    self.status.state = "failed"
                    self.status.session_expired = True
                    self.status.last_error = str(exc)
                    async with Notifier(cfg) as notifier:
                        await notifier.alert_session_expired(str(exc))
                    return
                except Exception as exc:  # noqa: BLE001 - one bad poll must not end the run
                    failures += 1
                    self._record_poll(
                        started, ok=False, error=f"{type(exc).__name__}: {exc}"
                    )
                    self.status.consecutive_failures = failures
                    self.status.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("Poll failed (%d consecutive)", failures)

                # One attempt per iteration, whether it succeeded or not, so the
                # count matches wall-clock intervals rather than only good polls.
                self.status.poll_count += 1

                # Measurement, not alerting: record when each listing first
                # shows up in a saved-search notification, so the push can be
                # compared against the poll. Every N polls, since it costs an
                # extra page load and Facebook notices a session that asks too
                # often.
                if (
                    cfg.notifications_probe
                    and self._session is not None
                    and self.status.poll_count % cfg.notifications_probe_every == 0
                ):
                    await self._probe_notifications()

                if failures >= 5 and self._session is not None:
                    log.error("5 consecutive failures — restarting browser")
                    await self._session.restart()
                    failures = 0
                    polls_since_restart = 0

                # Periodic restart to reclaim memory, as configured. A failed
                # restart is not fatal: the next poll fails, and the 5-failure
                # path above tries again.
                polls_since_restart += 1
                if (
                    cfg.restart_browser_every > 0
                    and polls_since_restart >= cfg.restart_browser_every
                    and self._session is not None
                ):
                    log.info("Restarting browser after %d polls", polls_since_restart)
                    try:
                        await self._session.restart()
                    except Exception:  # noqa: BLE001 - surfaced by the next poll
                        log.exception("Browser restart failed")
                    polls_since_restart = 0

                # Schedule the next poll `interval` after this one *started*, so
                # a slow page load doesn't stretch the cadence. Waiting the full
                # interval after each poll made a 30s setting behave like ~42s.
                elapsed = time.monotonic() - cycle_start
                await self._wait(max(0.0, wait - elapsed))
        finally:
            if self._session is not None:
                await self._session.stop()
                self._session = None
            if self.status.state != "failed":
                self.status.state = "stopped"
            self.status.next_poll_in = None
            log.info("Watcher loop stopped")

    async def _probe_notifications(self) -> None:
        """Record any listing ids newly visible in the notifications page.

        Never raises: this is a measurement riding along with the poll, and a
        failed probe must not be mistaken for a failed poll.
        """
        assert self._session is not None
        if self._notif_log is None:
            self._notif_log = NotificationLog(
                str(Path(self.cfg.db_path).parent / "notif-log.jsonl")
            )
        try:
            items = await fetch_alert_items(self._session)
            fresh = self._notif_log.record(items)
        except Exception:  # noqa: BLE001 - instrumentation must not break the loop
            log.exception("Notification probe failed")
            return
        if fresh:
            log.info(
                "Notification probe: %d new listing(s) in notifications: %s",
                len(fresh),
                ", ".join(item["id"] for item in fresh),
            )
        else:
            # Logged even when empty: without this, a probe that runs and finds
            # nothing is indistinguishable from one that never ran.
            log.debug(
                "Notification probe: %d listing(s) visible, none new", len(items)
            )

    async def _wait(self, seconds: float) -> None:
        """Sleep, honouring stop and 'poll now', while publishing the countdown."""
        wait = seconds + random.uniform(-self.cfg.jitter_seconds, self.cfg.jitter_seconds)
        wait = max(5.0, wait)
        deadline = time.monotonic() + wait
        while not self._stop_requested:
            if self._poll_now:
                self._poll_now = False
                self.status.next_poll_in = None
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.status.next_poll_in = None
                return
            self.status.next_poll_in = remaining
            await asyncio.sleep(min(0.5, remaining))

    async def _poll(self) -> int:
        """One scrape. Returns parsed count; raises on failure.

        History is written by the caller so that a failed poll is recorded
        too — a gap in the history would be indistinguishable from a stop.
        """
        cfg = self.cfg
        assert self._session is not None

        page = await open_search(self._session, cfg)
        try:
            cards, unparsed = await parse_cards(page, cfg.max_cards)
            # --debug keeps an HTML/screenshot of every empty or partial parse.
            # Without it, a dump is only written on a fully empty poll, so a
            # months-long run doesn't fill the disk with pages.
            if cfg.debug or not cards:
                await dump_debug(page, cfg.debug_dir, "no-cards")
            if unparsed:
                log.warning("%d card(s) could not be parsed", unparsed)
        finally:
            await page.close()

        matched = filters.apply(
            cards,
            require_any=cfg.require_any,
            exclude=cfg.exclude,
            min_price=cfg.search.min_price,
            max_price=cfg.search.max_price,
        )

        with Store(cfg.db_path) as store:
            first_run = store.is_empty
            silent = first_run and cfg.first_run_silent
            if first_run:
                log.info("First run — recording %d existing listing(s) silently", len(matched))

            new_listings: list[Listing] = []
            async with Notifier(cfg) as notifier:
                for listing in matched:
                    if not store.claim(listing, now=_utc_now(), alert=not silent):
                        continue
                    if not silent:
                        # The card badge always reads "Just listed", so the
                        # real age has to come from the detail page. One extra
                        # load per *new* listing, which is rare.
                        age = await fetch_listed_age(self._session, listing.url)
                        if age:
                            listing = listing.with_listed_text(age)
                            store.update_listed(listing.id, age, listing.listed_at)
                    new_listings.append(listing)
                    log.info(
                        "NEW  %s  %s  (%s)  listed %s",
                        listing.price_text,
                        listing.title,
                        listing.location,
                        listing.listed_display,
                    )
                    if not silent:
                        await notifier.send(listing)

        self.status.last_poll_at = _utc_now()
        self.status.last_error = None
        alerted = 0 if silent else len(new_listings)
        log.info("Poll: parsed=%d new=%d", len(cards), alerted)
        self.last_result = (len(cards), alerted)
        return len(cards)
