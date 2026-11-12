"""Read the Facebook notifications page for saved-search alerts.

A saved search is the only way to get Facebook to *push* a listing to us
instead of us polling for it. This module exists to measure whether that push
actually beats the poll: it records the first moment each listing id appears in
a notification, so `tools/notify_race.py` can compare that against the moment
the poll loop first saw the same id.

Nothing here alerts. It is instrumentation, and it is off unless
`notifications.probe` is enabled in config.yaml.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser import BrowserSession

log = logging.getLogger(__name__)

NOTIFICATIONS_URL = "https://www.facebook.com/notifications"

_ITEM_ID = re.compile(r"/marketplace/item/(\d+)")
# The notification row's own age, e.g. "5m", "2h", "3d", "1w". Facebook uses
# this compact form in the notification list, unlike the "2 hours ago" wording
# on a listing card.
_AGE = re.compile(r"\b(\d+)\s*([smhdw])\b")
_AGE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Walk up from the listing anchor to the notification row. The row is the
# nearest ancestor whose text carries a relative age; the anchor alone is just
# the thumbnail. Bounded so a layout change can't walk to <body>.
_EXTRACT_JS = """
() => {
    const out = [];
    const seen = new Set();
    for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
        const m = (a.getAttribute('href') || '').match(/\\/marketplace\\/item\\/(\\d+)/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        let node = a, text = '';
        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
            const t = (node.innerText || '').trim();
            if (/\\b\\d+\\s*[smhdw]\\b/.test(t) && t.length < 400) { text = t; break; }
        }
        if (!text) text = (a.innerText || '').trim();
        out.push({ id: id, text: text });
    }
    return out;
}
"""


# Any notification row at all, or a listing link: evidence the list rendered.
# Counted separately from the extractor's own match so "no saved-search alert"
# can be told apart from "the page never rendered".
_READY_JS = """
() => ({
    rows: document.querySelectorAll('[role="listitem"], [role="row"], [role="article"]').length,
    items: document.querySelectorAll('a[href*="/marketplace/item/"]').length,
})
"""

# The list is rendered client-side after the shell arrives; this bounds how
# long we wait for it rather than trusting one fixed sleep.
_READY_TIMEOUT_S = 20.0
_READY_POLL_MS = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_age(text: str) -> str:
    """Return the row's relative age ("5m") as normalised wording, or ""."""
    matches = _AGE.findall(text or "")
    if not matches:
        return ""
    number, unit = matches[-1]
    return f"{number}{unit}"


def _age_to_seconds(age_text: str) -> int | None:
    match = re.fullmatch(r"(\d+)([smhdw])", age_text or "")
    if not match:
        return None
    return int(match.group(1)) * _AGE_SECONDS[match.group(2)]


async def _read_rows(page: Page) -> list[dict]:
    try:
        return await page.evaluate(_EXTRACT_JS)
    except Exception as exc:  # noqa: BLE001 - a probe must never break the loop
        log.warning("Could not read notification rows: %s", exc)
        return []


async def _open_notifications(page: Page) -> dict:
    """Navigate to the notifications page and wait for the list to render.

    Returns the last readiness counts seen ({rows, items}).

    `domcontentloaded` is the wrong wait here: Facebook's app shell regularly
    takes longer than the whole navigation timeout to fire it, so Playwright
    raised and the caller threw away a page whose notifications were already
    in the DOM — which is how the probe came to report "0 listing(s) visible"
    on every single poll. `commit` is all we actually need; readiness is then
    polled for directly.
    """
    try:
        await page.goto(NOTIFICATIONS_URL, wait_until="commit")
    except PlaywrightTimeoutError:
        # The response started but never settled. The DOM after a partial load
        # is still worth reading, so this is not fatal.
        log.debug("Notifications navigation did not settle; reading what arrived")

    counts: dict = {}
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            counts = await page.evaluate(_READY_JS) or {}
        except Exception:  # noqa: BLE001 - mid-navigation the page can be blank
            counts = {}
        if counts.get("rows") or counts.get("items"):
            break
        await page.wait_for_timeout(_READY_POLL_MS)
    return counts


async def fetch_alert_items(session: BrowserSession) -> list[dict]:
    """Return the Marketplace listings currently visible in notifications.

    Each entry is {id, text, age_text}. Raises nothing: a probe failure is
    logged and reported as an empty list, because the poll loop must not die
    over a measurement.
    """
    page = await session.new_page()
    try:
        counts = await _open_notifications(page)
        await session.assert_logged_in(page)
        rows = await _read_rows(page)
    except Exception as exc:  # noqa: BLE001
        log.warning("Notification probe failed: %s", exc)
        return []
    finally:
        await page.close()

    items = []
    for row in rows:
        item_id = str(row.get("id") or "")
        if not item_id:
            continue
        text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        items.append({"id": item_id, "text": text, "age_text": _parse_age(text)})

    if not items:
        # The two zeroes mean opposite things: a page full of ordinary
        # notifications and no Marketplace alert is a saved search that is not
        # firing, while an empty page means the read itself is broken. Say
        # which one this was, so the next reader is not left guessing.
        if counts.get("rows"):
            log.info(
                "Notification probe: page rendered %d notification(s), none of "
                "them a Marketplace saved-search alert",
                counts["rows"],
            )
        else:
            log.warning(
                "Notification probe: notifications page rendered no rows at all "
                "— no alerts, or the read is not reaching the list"
            )
    return items


class NotificationLog:
    """Append-only record of the first time each listing id was seen in a push.

    A JSONL file rather than a table: this is a measurement, not bot state, and
    keeping it out of the database means the schema stays about listings.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._seen.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    continue
        except OSError as exc:
            log.warning("Could not read notification log %s: %s", self.path, exc)

    def record(self, items: list[dict]) -> list[dict]:
        """Append any ids not seen before. Returns the newly recorded entries."""
        fresh = [item for item in items if item["id"] not in self._seen]
        if not fresh:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _utc_now()
        with self.path.open("a", encoding="utf-8") as handle:
            for item in fresh:
                entry = {
                    "id": item["id"],
                    "first_seen": now,
                    "age_text": item.get("age_text", ""),
                    "text": item.get("text", ""),
                }
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._seen.add(item["id"])
        return fresh
