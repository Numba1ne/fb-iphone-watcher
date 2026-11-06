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
"""


class Store:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
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
