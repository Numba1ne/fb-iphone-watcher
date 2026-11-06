"""SQLite-backed record of listings we have already seen."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .models import Listing

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    price      INTEGER,
    location   TEXT,
    url        TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    notified   INTEGER NOT NULL DEFAULT 0
);

-- One row per scrape. The dashboard reads these to answer "is it working?"
-- and to draw the poll history, without parsing the log file.
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    parsed     INTEGER NOT NULL DEFAULT 0,
    new_count  INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_polls_started ON polls(started_at);
"""

# Wall-to-wall polling would bloat the DB; a fortnight is plenty for a dashboard.
POLL_RETENTION_DAYS = 14


class Store:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def is_empty(self) -> bool:
        return self._conn.execute("SELECT 1 FROM listings LIMIT 1").fetchone() is None

    def claim(self, listing: Listing, *, now: str, alert: bool) -> bool:
        """Insert a listing; return True only if it was not already known.

        `INSERT OR IGNORE` makes the "is this new?" check atomic — a restart
        mid-loop can't re-notify something already in the table.

        `alert` records whether we *tried* to notify: True for a live alert,
        False for a first-run (silent) record. It is informational; dedupe is
        by primary key, so either value means "seen".
        """
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO listings "
            "(id, title, price, location, url, first_seen, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                listing.id,
                listing.title,
                listing.price,
                listing.location,
                listing.url,
                now,
                int(alert),
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    # ------------------------------------------------------- dashboard reads

    def record_poll(
        self, *, started_at: str, finished_at: str, parsed: int, new: int, ok: bool, error: str = ""
    ) -> None:
        self._conn.execute(
            "INSERT INTO polls (started_at, finished_at, parsed, new_count, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (started_at, finished_at, parsed, new, int(ok), error or None),
        )
        self._conn.execute(
            "DELETE FROM polls WHERE started_at < datetime('now', ?)",
            (f"-{POLL_RETENTION_DAYS} days",),
        )
        self._conn.commit()

    def recent_listings(self, limit: int = 200, *, only_new: bool = False) -> list[dict]:
        where = "WHERE notified = 1" if only_new else ""
        rows = self._conn.execute(
            f"SELECT id, title, price, location, url, first_seen, notified "
            f"FROM listings {where} ORDER BY rowid DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def recent_polls(self, limit: int = 40) -> list[dict]:
        rows = self._conn.execute(
            "SELECT started_at, finished_at, parsed, new_count, ok, error "
            "FROM polls ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN notified = 1 THEN 1 ELSE 0 END) AS alerted, "
            "MIN(price) AS min_price, MAX(price) AS max_price, "
            "MIN(first_seen) AS first_seen "
            "FROM listings"
        ).fetchone()
        last = self._conn.execute(
            "SELECT finished_at, ok FROM polls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "total": row["total"] or 0,
            "alerted": row["alerted"] or 0,
            "min_price": row["min_price"],
            "max_price": row["max_price"],
            "first_seen": row["first_seen"],
            "last_poll_at": last["finished_at"] if last else None,
            "last_poll_ok": bool(last["ok"]) if last else None,
        }
