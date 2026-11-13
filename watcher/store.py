"""SQLite-backed record of listings we have already seen."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .models import Listing

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    price       INTEGER,
    location    TEXT,
    url         TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,
    -- Facebook's own wording ("2 hours ago") plus the absolute moment it
    -- implies, estimated when we scraped the card.
    listed_text TEXT,
    listed_at   TEXT
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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a
        long-running install would otherwise never gain the new columns.
        """
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(listings)")}
        for column in ("listed_text", "listed_at"):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")

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

        `alert` sets the row's initial notified flag. Callers that claim before
        deciding (the poll claims, then reads the age before alerting) should
        pass False and call `set_notified(..., True)` once a send has actually
        succeeded; otherwise the dashboard briefly advertises an alert that the
        age gate then drops.
        """
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO listings "
            "(id, title, price, location, url, first_seen, notified, listed_text, listed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                listing.id,
                listing.title,
                listing.price,
                listing.location,
                listing.url,
                now,
                int(alert),
                listing.listed_text or None,
                listing.listed_at,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def set_notified(self, listing_id: str, notified: bool) -> None:
        """Record whether an alert actually reached a channel.

        Rows are inserted as unseen; this flips them to told once a send
        succeeds, so the dashboard's ALERTED chip and count mean "delivered"
        rather than "we meant to".
        """
        self._conn.execute(
            "UPDATE listings SET notified = ? WHERE id = ?",
            (int(notified), listing_id),
        )
        self._conn.commit()

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
        # datetime() normalises both sides: started_at is ISO-8601 with a "T"
        # and a +00:00 offset, which does not compare correctly as a string
        # against SQLite's "YYYY-MM-DD HH:MM:SS" output.
        self._conn.execute(
            "DELETE FROM polls WHERE datetime(started_at) < datetime('now', ?)",
            (f"-{POLL_RETENTION_DAYS} days",),
        )
        self._conn.commit()

    def search_listings(
        self,
        *,
        limit: int = 200,
        only_new: bool = False,
        min_price: int | None = None,
        max_price: int | None = None,
        q: str = "",
        since_hours: float | None = None,
    ) -> tuple[list[dict], int]:
        """Return (page of listings, total matching the filters).

        Filtering happens in SQL so `total` counts every match rather than
        only the rows that happened to fit in the page.

        `since_hours` keeps only listings first seen within that window. The
        first run records the whole existing backlog silently, and without a
        window those old listings sit at the bottom of the dashboard forever,
        making a working watcher look like it is stuck on stale results.
        """
        clauses: list[str] = []
        params: list[object] = []

        if only_new:
            clauses.append("notified = 1")
        if since_hours is not None:
            clauses.append("datetime(first_seen) >= datetime('now', ?)")
            params.append(f"-{since_hours} hours")
        if min_price is not None:
            clauses.append("price IS NOT NULL AND price >= ?")
            params.append(min_price)
        if max_price is not None:
            clauses.append("price IS NOT NULL AND price <= ?")
            params.append(max_price)

        needle = q.strip().lower()
        if needle:
            # Escape LIKE's own wildcards so a search for "50%" means "50%".
            escaped = (
                needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append(
                "(LOWER(title) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(location, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{escaped}%", f"%{escaped}%"])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM listings {where}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT id, title, price, location, url, first_seen, notified, "
            f"listed_text, listed_at "
            f"FROM listings {where} ORDER BY rowid DESC LIMIT ?",
            [*params, limit],
        )
        return [dict(row) for row in rows], total

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
