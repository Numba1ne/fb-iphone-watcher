"""Owns the polling loop as a startable/stoppable task.

The loop and the web server run in one process and one event loop, so this
object is the single source of truth for "is it running?". Nothing infers
state from a log line or a PID file, which is how these two get to disagree.

Design note on speed: the poll interval is only half the story. A poll cannot
run more often than it takes to complete, so this module also keeps the work
per poll small — one page load per search, one batched parse, and a browser
restart every so many polls to stop a long-lived Chromium going stale.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import filters
from .browser import BrowserSession, SessionExpired
from .marketplace import (
    dump_debug,
    open_search,
    parse_cards,
    read_creation_times,
    stamp_created,
)
from .models import AppConfig, Listing
from .notifications import NotificationLog, fetch_alert_items
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)

# Matches the price inside a notification's text ("New listing: iPhone 13 £280").
_NOTIF_PRICE = re.compile(r"£\s*([\d,]+)")


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
        # True until the first poll that actually reads listings. That poll
        # records whatever is already on the search page without alerting: after
        # an overnight stop the page is full of yesterday's listings, and
        # announcing them is noise, not news. Cleared once a poll has claimed
        # them, so a failed first poll doesn't consume the warm-up.
        self._startup_poll = True

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
        if not await self._start_browser():
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

                if failures >= 5:
                    log.error("%d consecutive failures — restarting browser", failures)
                    try:
                        if self._session is not None:
                            await self._session.restart()
                        else:
                            await self._start_browser()
                        failures = 0
                        polls_since_restart = 0
                    except Exception:  # noqa: BLE001 - next iteration retries
                        log.exception("Browser restart failed after repeated poll failures")
                        self._session = None

                # Periodic restart to reclaim memory and shed any per-profile
                # staleness. A failed restart is not fatal: the next poll fails
                # and the failure path above tries again.
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
                        self._session = None
                    polls_since_restart = 0

                # A heartbeat every poll: with a 20s cadence the log would
                # otherwise be silent for long stretches, and a silent log is
                # indistinguishable from a hung process at 3am.
                log.info(
                    "HEARTBEAT poll=%d state=%s last_poll=%s failures=%d next_in=%.0fs",
                    self.status.poll_count,
                    self.status.state,
                    self.status.last_poll_at,
                    failures,
                    wait,
                )

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

    async def _start_browser(self) -> bool:
        """Start the browser, reporting a failure in the UI rather than raising."""
        try:
            self._session = BrowserSession(self.cfg)
            await self._session.start()
            return True
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            log.exception("Could not start browser")
            self._session = None
            self.status.state = "failed"
            self.status.last_error = f"Browser failed to start: {exc}"
            return False

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

    # ------------------------------------------------------------------ poll

    def _search_urls(self) -> list[tuple[str, str]]:
        """Return [(label, url)] — the primary search plus an optional second.

        The second search differs slightly (wider radius and/or no price
        ceiling) so the two together surface listings either one alone would
        miss. Results are merged and de-duplicated by listing id.
        """
        cfg = self.cfg
        searches = [("primary", cfg.search.build_url())]
        if cfg.second_search:
            alt = cfg.search.build_url(
                radius_km=cfg.second_search_radius_km,
                drop_max_price=cfg.second_search_drop_max_price,
            )
            if alt != searches[0][1]:
                searches.append(("secondary", alt))
        return searches

    async def _fetch_cards(
        self, label: str, url: str
    ) -> tuple[list[Listing], int | None]:
        """Load one search in its own fresh page and parse it.

        Returns the cards and Facebook's own `server_time` if the page carried
        one, so the freshness gate can measure against Facebook's clock rather
        than this machine's. None means no card could be dated.
        """
        assert self._session is not None
        cfg = self.cfg
        render_started = time.monotonic()
        page = await open_search(self._session, cfg, url=url)
        try:
            cards, unparsed = await parse_cards(page, cfg.max_cards)
            # The page's JSON carries each card's exact posting moment, so the
            # age comes with the card and no listing is opened a second time.
            posted, server_time = await read_creation_times(page)
            # How long the page took to render and read. The gate counts those
            # seconds too, so a slow page cannot make its listings look newer
            # than they are.
            render_delay = time.monotonic() - render_started
            if render_delay >= 60:
                log.warning(
                    "%s search: page took %.0fs to render and read; its "
                    "timestamps are dated that much before the poll",
                    label,
                    render_delay,
                )
            age_clock: int | None = None
            read_at: float | None = None
            if cards and posted:
                # Facebook's own clock dates the listing, so it dates the age:
                # the stamps are read the moment the page renders, which is
                # `server_time`. A page without one dates on the local clock,
                # so a skew shows up as a plainly wrong age in the log rather
                # than passing unremarked.
                age_clock = server_time or int(time.time())
                cards = stamp_created(cards, posted, age_clock)
                read_at = time.monotonic() - render_delay
            elif cards:
                # Ages drive the freshness gate, so losing them silently would
                # stop every alert while the log still looked healthy.
                log.warning(
                    "%s search: page carried no listing timestamps — ages "
                    "unknown, so nothing can be proven fresh",
                    label,
                )
            # --debug keeps an HTML/screenshot of every empty or partial parse.
            # Without it, a dump is only written on a fully empty poll, so a
            # months-long run doesn't fill the disk with pages.
            if cfg.debug or not cards:
                await dump_debug(page, cfg.debug_dir, f"no-cards-{label}")
            if unparsed:
                log.warning("%s search: %d card(s) could not be parsed", label, unparsed)
            return cards, read_at
        finally:
            await page.close()

    async def _poll(self) -> int:
        """One scrape. Returns parsed count; raises on failure.

        History is written by the caller so that a failed poll is recorded
        too — a gap in the history would be indistinguishable from a stop.
        """
        cfg = self.cfg
        assert self._session is not None

        searches = self._search_urls()
        results = await asyncio.gather(
            *(self._fetch_cards(label, url) for label, url in searches),
            return_exceptions=True,
        )

        cards: list[Listing] = []
        seen: set[str] = set()
        errors: list[str] = []
        # The monotonic moment the page's timestamps were read. The gate ages
        # each listing from this moment, so the seconds spent claiming and
        # deciding still count — a listing must never get younger while it
        # waits to be judged.
        read_wall: float | None = None
        for (label, _url), result in zip(searches, results):
            if isinstance(result, BaseException):
                errors.append(f"{label}: {result}")
                continue
            found, read_at = result
            # Earliest wins: the cards were stamped against whichever page
            # reached us first, and counting from a later moment would make
            # them younger than their stamps.
            if read_at is not None and (read_wall is None or read_at < read_wall):
                read_wall = read_at
            for card in found:
                if card.id in seen:
                    continue
                seen.add(card.id)
                # A card without a timestamp is only as good as the clock the
                # page carries; anchor it there so the gate below never has to
                # fall back to this machine's clock.
                cards.append(
                    card if read_at is None else card.with_created_clock(int(read_at))
                )

        if errors:
            for err in errors:
                log.warning("Search failed — %s", err)
            # One search failing is survivable; both failing is a failed poll.
            if not cards:
                raise RuntimeError("; ".join(errors))

        matched = filters.apply(
            cards,
            require_any=cfg.require_any,
            exclude=cfg.exclude,
            min_price=cfg.search.min_price,
            max_price=cfg.search.max_price,
        )

        alerted = 0
        with Store(cfg.db_path) as store:
            # The first poll after every start is a warm-up, not just the very
            # first run against an empty database. Anything already on the page
            # when the bot comes up is recorded, shown on the dashboard, and
            # never announced — only listings that appear afterwards are news.
            silent = self._startup_poll and cfg.first_run_silent

            # Claim first: claim() is the atomic "have we seen this?" check, so
            # it must happen before any slow work. `fresh` is the rare set of
            # listings that are genuinely new to us. They are claimed as *not*
            # yet told: the row only flips to notified=1 once a send actually
            # succeeds, so the dashboard never shows ALERTED for a listing the
            # age gate is about to drop.
            fresh = [
                listing
                for listing in matched
                if store.claim(listing, now=_utc_now(), alert=False)
            ]
            if matched:
                self._startup_poll = False

            # Ages arrive with the cards themselves (see _fetch_cards), so a
            # poll that finds nine new listings no longer opens nine detail
            # pages to date them. That fan-out was the largest remaining cost
            # per poll, and the extra page loads were the likeliest reason
            # Facebook started resetting connections.
            #
            def _proven_fresh(minutes: int | None) -> bool:
                """Only an age we actually read can prove a listing is fresh.

                An unreadable age is not evidence, and during a warm-up
                "not proven" must mean "stay quiet" — otherwise one timeout
                replay the whole backlog, which is the bug this replaced.
                """
                return (
                    cfg.max_listed_minutes > 0
                    and minutes is not None
                    and minutes <= cfg.max_listed_minutes
                )

            async with Notifier(cfg) as notifier:
                for listing in fresh:
                    # The card's own timestamp when the page carried one; only
                    # a card without one falls back to its badge wording, and a
                    # card with neither reads as unproven rather than as new —
                    # that distinction is what stopped an overnight backlog
                    # being announced on restart. The age is read against the
                    # clock the page itself was dated by — advanced to now, so
                    # the seconds spent parsing and claiming still count — and
                    # never against this machine's clock, which is not the one
                    # Facebook wrote the timestamps with.
                    elapsed = (
                        0.0
                        if read_wall is None or listing.age_clock_epoch is None
                        else time.monotonic() - read_wall
                    )
                    minutes = listing.aged_now(max(0.0, elapsed)).minutes_old()

                    if silent:
                        # Warm-up: announce only what a readable age proves was
                        # posted while the bot was down. Yesterday's listings and
                        # anything unreadable stay quiet, visible on the
                        # dashboard under All seen.
                        if not _proven_fresh(minutes):
                            log.info(
                                "RECORDED (startup)  %s  %s  (%s)",
                                listing.title,
                                listing.price_text,
                                listing.location,
                            )
                            continue
                        log.info(
                            "ALERT (age: %d min, posted while stopped)  %s  %s  (%s)",
                            minutes,
                            listing.title,
                            listing.price_text,
                            listing.location,
                        )
                        if await notifier.send(listing):
                            store.set_notified(listing.id, True)
                            alerted += 1
                        continue

                    # Freshness gate: if the listing's own age says it is older
                    # than the threshold, it is not what we are racing for.
                    # Record it, but stay quiet.
                    if (
                        cfg.max_listed_minutes > 0
                        and minutes is not None
                        and minutes > cfg.max_listed_minutes
                    ):
                        log.info(
                            "SKIPPED (too old: %d min)  %s  %s  (%s)",
                            minutes,
                            listing.title,
                            listing.price_text,
                            listing.location,
                        )
                        continue

                    log.info(
                        "ALERT (age: %s)  %s  %s  (%s)",
                        "unknown" if minutes is None else f"{minutes} min",
                        listing.title,
                        listing.price_text,
                        listing.location,
                    )
                    if await notifier.send(listing):
                        store.set_notified(listing.id, True)
                        alerted += 1

            if silent:
                # Both counts, because they answer different questions: how much
                # the page holds, and how much of it was not already known.
                log.info(
                    "Startup — warm-up poll: %d listing(s) on the page, %d new to "
                    "us, %d announced; the rest were already listed",
                    len(matched),
                    len(fresh),
                    alerted,
                )

        self.status.last_poll_at = _utc_now()
        self.status.last_error = None
        log.info("Poll: parsed=%d new=%d", len(cards), alerted)
        self.last_result = (len(cards), alerted)
        return len(cards)

    # --------------------------------------------------------------- probe

    async def _probe_notifications(self) -> None:
        """Record any listing ids newly visible in the notifications page.

        Never raises: this is a measurement riding along with the poll, and a
        failed probe must not be mistaken for a failed poll. When
        `alert_from_probe` is on it also fires an early alert, which is the
        whole reason the saved search exists — Facebook's push can surface a
        listing before our own search request sees it.
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

        if not fresh:
            # Logged even when empty: without this, a probe that runs and finds
            # nothing is indistinguishable from one that never ran.
            log.debug(
                "Notification probe: %d listing(s) visible, none new", len(items)
            )
            return

        log.info(
            "Notification probe: %d new listing(s) in notifications: %s",
            len(fresh),
            ", ".join(item["id"] for item in fresh),
        )
        if self.cfg.alert_from_probe:
            await self._alert_from_probe(fresh)

    async def _alert_from_probe(self, fresh: list[dict]) -> None:
        """Alert on listings the notifications page surfaced first.

        The notification's text is all we have, so the same title filters are
        applied to it; anything they reject is left for the search poll to
        judge properly. A claimed id is also how we stop the next poll from
        alerting the same listing a second time.
        """
        cfg = self.cfg
        async with Notifier(cfg) as notifier:
            for item in fresh:
                text = item.get("text") or ""
                price_match = _NOTIF_PRICE.search(text)
                listing = Listing.from_card(
                    item_id=str(item["id"]),
                    title=text or "Marketplace listing",
                    price=(
                        int(price_match.group(1).replace(",", ""))
                        if price_match
                        else None
                    ),
                    location="saved-search alert",
                    listed_text=item.get("age_text") or "",
                )
                matched = filters.apply(
                    [listing],
                    require_any=cfg.require_any,
                    exclude=cfg.exclude,
                )
                if not matched:
                    continue
                with Store(cfg.db_path) as store:
                    if not store.claim(listing, now=_utc_now(), alert=False):
                        continue
                    log.info("PROBE NEW  %s  (from notification)", listing.title)
                    if await notifier.send(listing):
                        store.set_notified(listing.id, True)
