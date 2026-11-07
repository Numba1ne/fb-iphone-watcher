"""Local dashboard: static page plus a small JSON API.

Bound to 127.0.0.1 and never exposed. The Host-header check blocks DNS
rebinding, where a malicious page resolves its own domain to 127.0.0.1 to
reach this server with the browser's privileges.
"""

from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .models import AppConfig
from .runner import WatcherRunner
from .store import Store

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _host_ok(request: Request) -> bool:
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host in ALLOWED_HOSTS


def create_app(cfg: AppConfig, runner: WatcherRunner, *, autostart: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart:
            await runner.start()
        yield
        await runner.stop(timeout=15)

    api = FastAPI(title="iPhone Watcher", docs_url=None, redoc_url=None, lifespan=lifespan)

    @api.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_ok(request):
            return JSONResponse({"detail": "Bad host"}, status_code=421)
        return await call_next(request)

    @api.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        # Read per request so edits to the page show on reload without a restart.
        # No-cache: a stale dashboard against a changed API is a confusing bug.
        return HTMLResponse(
            (STATIC / "index.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @api.get("/api/status")
    async def status() -> dict:
        s = runner.status.as_dict()
        with Store(cfg.db_path) as store:
            s["stats"] = store.stats()
        return s

    @api.get("/api/listings")
    async def listings(
        limit: int = Query(100, ge=1, le=500),
        only_new: bool = False,
        max_price: int | None = None,
        min_price: int | None = None,
        q: str = "",
        since_hours: float | None = Query(None, ge=0),
    ) -> dict:
        with Store(cfg.db_path) as store:
            rows, total = store.search_listings(
                limit=limit,
                only_new=only_new,
                min_price=min_price,
                max_price=max_price,
                q=q,
                since_hours=since_hours,
            )
        return {"items": rows, "total": total}

    @api.get("/api/polls")
    async def polls(limit: int = Query(40, ge=1, le=200)) -> dict:
        with Store(cfg.db_path) as store:
            return {"items": store.recent_polls(limit)}

    @api.get("/api/settings")
    async def settings() -> dict:
        s = cfg.search
        return {
            "query": s.query,
            "city_slug": s.city_slug,
            "radius_km": s.radius_km,
            "min_price": s.min_price,
            "max_price": s.max_price,
            "days_since_listed": s.days_since_listed,
            "interval_seconds": cfg.interval_seconds,
            "jitter_seconds": cfg.jitter_seconds,
            "max_cards": cfg.max_cards,
            "headless": cfg.headless,
            "telegram": cfg.telegram,
            "discord": cfg.discord,
            "first_run_silent": cfg.first_run_silent,
            "require_any": list(cfg.require_any),
            "exclude": list(cfg.exclude),
            "search_url": s.build_url(),
        }

    @api.post("/api/start")
    async def start() -> dict:
        if runner.running:
            return {"started": False, "state": runner.status.state, "reason": "already running"}
        # Starting clears a stale session error so the UI doesn't keep showing it
        # after the user has run --login.
        await runner.start()
        return {"started": True, "state": runner.status.state}

    @api.post("/api/stop")
    async def stop() -> dict:
        was = runner.running
        await runner.stop()
        return {"stopped": was, "state": runner.status.state}

    @api.post("/api/poll-now")
    async def poll_now() -> dict:
        if not runner.running:
            raise HTTPException(409, "The watcher is stopped. Start it first.")
        runner.trigger_poll()
        return {"ok": True}

    @api.get("/api/export.csv")
    async def export_csv() -> Response:
        import csv
        import io

        with Store(cfg.db_path) as store:
            rows, _ = store.search_listings(limit=5000, only_new=True)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["id", "title", "price", "location", "url", "listed_text", "listed_at", "first_seen"]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["title"],
                    row["price"],
                    row["location"],
                    row["url"],
                    row["listed_text"],
                    row["listed_at"],
                    row["first_seen"],
                ]
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        # A real file download, not a JSON envelope: the browser saves it
        # directly, which is what an "Export CSV" button has to do.
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="iphone-listings-{stamp}.csv"'
            },
        )

    return api


async def serve(
    cfg: AppConfig, runner: WatcherRunner, *, port: int = 8765, open_browser: bool = True
) -> None:
    import uvicorn

    api = create_app(cfg, runner)
    config = uvicorn.Config(
        api,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    url = f"http://127.0.0.1:{port}"

    async def open_when_ready() -> None:
        # Poll the socket rather than sleeping a guessed amount, so the tab
        # never opens onto a connection refused.
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        log.info("Dashboard: %s", url)
        print(f"\n  Dashboard: {url}   (Ctrl+C to stop)\n", flush=True)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - a browser failing must not stop the server
                log.warning("Could not open a browser automatically — visit %s", url)

    opener = asyncio.create_task(open_when_ready())
    try:
        await server.serve()
    finally:
        opener.cancel()
