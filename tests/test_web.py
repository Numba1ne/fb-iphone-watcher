"""Dashboard API: routes, host guard, filters, and start/stop wiring.

Runs against the real FastAPI app with the watcher stubbed, so it needs no
browser and no Facebook access.

Run:  python -m tests.test_web
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import watcher.runner as runner_mod  # noqa: E402
from tests.test_notify import base_config  # noqa: E402
from watcher.models import Listing  # noqa: E402
from watcher.runner import WatcherRunner  # noqa: E402
from watcher.store import Store  # noqa: E402
from watcher.web import create_app  # noqa: E402


class FakeSession:
    async def start(self): ...
    async def stop(self): ...
    async def restart(self): ...


class FakePage:
    """Empty parses trigger the debug dump, which needs these two methods."""

    async def close(self): ...
    async def content(self): return "<html></html>"
    async def screenshot(self, **kwargs): ...


def seed(db_path: str) -> None:
    with Store(db_path) as store:
        for l_id, title, price, loc, alert in [
            ("10", "iPhone 13 128GB", 280, "Croydon, London", True),
            ("11", "iPhone 14 for parts", 150, "Epsom", True),
            ("12", "iPhone 15 256GB", 900, "Sutton", False),
        ]:
            store.claim(Listing.from_card(l_id, title, price, loc), now="2026-11-06T12:00:00", alert=alert)
        store.record_poll(started_at="2026-11-06T12:00:00", finished_at="2026-11-06T12:00:05",
                          parsed=24, new=2, ok=True)
        store.record_poll(started_at="2026-11-06T12:01:00", finished_at="2026-11-06T12:01:04",
                          parsed=0, new=0, ok=False, error="network down")


def main() -> int:
    failures = []

    def check(label: str, condition: bool) -> None:
        print(f"  {'PASS ' if condition else 'FAIL '} {label}")
        if not condition:
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = replace(base_config(telegram=True, discord=True),
                      db_path=str(Path(tmpdir) / "bot.db"),
                      debug_dir=str(Path(tmpdir) / "debug"))
        seed(cfg.db_path)
        runner = WatcherRunner(cfg)
        app = create_app(cfg, runner, autostart=False)

        async def fake_open(session, cfg):
            return FakePage()

        async def fake_parse(page, limit):
            return [], 0

        with patch.object(runner_mod, "BrowserSession", lambda cfg: FakeSession()), \
             patch.object(runner_mod, "open_search", fake_open), \
             patch.object(runner_mod, "parse_cards", fake_parse):
            with TestClient(app, base_url="http://127.0.0.1") as client:
                # --- host guard (DNS rebinding)
                r = client.get("/api/status", headers={"host": "evil.example.com"})
                check("rejects foreign Host header (421)", r.status_code == 421)
                r = client.get("/api/status", headers={"host": "localhost:8765"})
                check("allows localhost Host header", r.status_code == 200)

                # --- page
                r = client.get("/")
                check("serves the dashboard page", r.status_code == 200 and "iPhone Watcher" in r.text)

                # --- status
                s = client.get("/api/status").json()
                check(f"status reports stopped (got {s['state']})", s["state"] == "stopped")
                check(f"stats count all listings (got {s['stats']['total']})", s["stats"]["total"] == 3)
                check(f"stats count alerted only (got {s['stats']['alerted']})", s["stats"]["alerted"] == 2)
                check("stats carry last poll", s["stats"]["last_poll_at"] is not None)

                # --- listings
                all_rows = client.get("/api/listings?limit=10").json()
                check(f"listings default to all seen (got {all_rows['total']})", all_rows["total"] == 3)
                new_rows = client.get("/api/listings?only_new=true").json()
                check(f"only_new filters to alerted (got {new_rows['total']})", new_rows["total"] == 2)
                # Seeded prices are 280, 150 and 900.
                cheap = client.get("/api/listings?max_price=300").json()
                check(f"max_price excludes the £900 listing (got {cheap['total']})", cheap["total"] == 2)
                tight = client.get("/api/listings?max_price=200").json()
                check(f"max_price excludes above it (got {tight['total']})", tight["total"] == 1)
                searched = client.get("/api/listings?q=epsom").json()
                check(f"search matches location (got {searched['total']})", searched["total"] == 1)
                check("listings newest first", all_rows["items"][0]["id"] == "12")

                # --- polls
                p = client.get("/api/polls").json()["items"]
                check(f"poll history returned (got {len(p)})", len(p) == 2)
                check("failure recorded in history", p[0]["ok"] == 0 and p[0]["error"] == "network down")

                # --- settings
                st = client.get("/api/settings").json()
                check("settings expose the search URL", "marketplace/london/search" in st["search_url"])
                check("settings never expose secrets", "token" not in str(st).lower())

                # --- controls
                r = client.post("/api/poll-now")
                check("poll-now refused while stopped (409)", r.status_code == 409)

                r = client.post("/api/start").json()
                check("start begins the runner", r["started"] is True)
                check("status flips to running", client.get("/api/status").json()["state"] == "running")

                r = client.post("/api/start").json()
                check("start is idempotent", r["started"] is False)

                r = client.post("/api/poll-now")
                check("poll-now accepted while running", r.status_code == 200)

                r = client.post("/api/stop").json()
                check("stop halts the runner", r["stopped"] is True)
                check("status flips back to stopped", client.get("/api/status").json()["state"] == "stopped")

                # --- export
                csv = client.get("/api/export.csv").json()["csv"]
                check("csv has a header and alerted rows only", csv.startswith("id,title") and len(csv.splitlines()) == 3)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all web checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
