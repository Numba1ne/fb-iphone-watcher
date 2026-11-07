"""Notification delivery, tested against a local mock HTTP server.

Verifies formatting, retry behaviour, fail-fast on 4xx, and that one broken
channel never blocks the other. No real tokens or network needed.

Run:  python tests/test_notify.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

import watcher.notify as notify_mod  # noqa: E402
from watcher.models import AppConfig, Listing, SearchSpec  # noqa: E402
from watcher.notify import Notifier  # noqa: E402

received: list[dict] = []
attempts = {"flaky": 0}


async def _ok(request: web.Request) -> web.Response:
    received.append(await request.json())
    return web.Response(text="ok")


async def _flaky(request: web.Request) -> web.Response:
    attempts["flaky"] += 1
    if attempts["flaky"] < 3:
        return web.Response(status=503)
    received.append(await request.json())
    return web.Response(text="ok")


async def _unauthorized(request: web.Request) -> web.Response:
    return web.Response(status=401, text="Unauthorized")


def base_config(**overrides: object) -> AppConfig:
    cfg = AppConfig(
        search=SearchSpec("london", "iphone", 100, 1200, 25, 51.5, -0.12),
        interval_seconds=45,
        jitter_seconds=0,
        max_cards=30,
        restart_browser_every=100,
        headless=True,
        user_data_dir="",
        navigation_timeout_ms=30000,
        require_any=("iphone",),
        exclude=(),
        telegram=False,
        discord=False,
        first_run_silent=True,
        db_path="",
        log_path="",
        debug_dir="",
        telegram_token="TEST",
        telegram_chat_id="42",
        discord_webhook_url="",
    )
    return replace(cfg, **overrides)


LISTING = Listing.from_card("123", "iPhone 13 128GB <Blue>", 280, "Croydon, London")


async def main() -> int:
    app = web.Application()
    app.router.add_post("/ok", _ok)
    app.router.add_post("/flaky", _flaky)
    app.router.add_post("/401", _unauthorized)
    runner = web.AppRunner(app)
    await runner.setup()
    # Port 0: let the OS pick a free port. A fixed 8765 collides with a running
    # dashboard, which would make this suite fail for an unrelated reason.
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    ok_url = f"{base_url}/ok"
    failures = []

    def check(label: str, condition: bool) -> None:
        print(f"  {'PASS ' if condition else 'FAIL '} {label}")
        if not condition:
            failures.append(label)

    # a) Telegram message formatting + HTML escaping
    async with Notifier(base_config(telegram=True, telegram_chat_id="42")) as nt:
        sent = await nt._post(
            ok_url,
            {"chat_id": "42", "text": notify_mod._telegram_text(LISTING), "parse_mode": "HTML"},
            label="Telegram",
        )
    text = received[-1]["text"]
    check("telegram send succeeded", sent)
    check("telegram shows price", "£280" in text)
    check("telegram shows location", "Croydon, London" in text)
    check("telegram shows direct link", LISTING.url in text)
    check("telegram escapes <Blue>", "&lt;Blue&gt;" in text)

    # b) Discord embed shape
    dc = base_config(discord=True, discord_webhook_url=ok_url)
    async with Notifier(dc) as nt:
        await nt._discord_listing(LISTING)
    embed = received[-1]["embeds"][0]
    check("discord embed titled", embed["title"] == LISTING.title)
    check("discord embed links to listing", embed["url"] == LISTING.url)
    check("discord embed shows price", embed["description"] == "**£280**")

    # c) 503 retried, then succeeds
    attempts["flaky"] = 0
    flaky = base_config(discord=True, discord_webhook_url=f"{base_url}/flaky")
    async with Notifier(flaky) as nt:
        result = await nt._discord_message("hi")
    check(f"retries 503 and succeeds (attempts={attempts['flaky']})", result and attempts["flaky"] == 3)

    # d) 401 fails fast, no retry
    bad = base_config(discord=True, discord_webhook_url=f"{base_url}/401")
    started = time.monotonic()
    async with Notifier(bad) as nt:
        result = await nt._discord_message("hi")
    check("401 returns failure without retrying", result is False and time.monotonic() - started < 2)

    # e) one channel raising must not stop the other
    original = notify_mod.Notifier._telegram_message

    async def boom(self: Notifier, text: str) -> bool:
        raise RuntimeError("telegram exploded")

    notify_mod.Notifier._telegram_message = boom
    try:
        both = base_config(telegram=True, discord=True, discord_webhook_url=ok_url)
        async with Notifier(both) as nt:
            result = await nt.send(LISTING)
        check("discord still delivers when telegram raises", result is True)
        check("discord payload actually arrived", received[-1]["embeds"][0]["url"] == LISTING.url)
    finally:
        notify_mod.Notifier._telegram_message = original

    # f) A Discord-only setup still strips the HTML tags we emit.
    dc_only = base_config(telegram=False, discord=True, discord_webhook_url=ok_url)
    async with Notifier(dc_only) as nt:
        await nt._discord_message(
            "<b>Facebook session expired</b>\n<code>python main.py --login</code>"
        )
    content = received[-1]["content"]
    check(
        "discord-only alert has no raw tags",
        "<b>" not in content and "<code>" not in content,
    )

    # g) alert_session_expired reaches a channel with notification: true even
    #    though no token is configured — this is the "test both channels
    #    without credentials" path the README describes.
    no_creds = base_config(telegram=False, discord=True, discord_webhook_url="")
    captured: list[str] = []
    original_telegram, original_discord = (
        notify_mod.Notifier._telegram_message,
        notify_mod.Notifier._discord_message,
    )

    async def fake_telegram(self: Notifier, text: str) -> bool:
        captured.append("telegram")
        return True

    async def fake_discord(self: Notifier, text: str) -> bool:
        captured.append("discord")
        return True

    notify_mod.Notifier._telegram_message = fake_telegram
    notify_mod.Notifier._discord_message = fake_discord
    try:
        async with Notifier(no_creds) as nt:
            await nt.alert_session_expired("test")
    finally:
        notify_mod.Notifier._telegram_message = original_telegram
        notify_mod.Notifier._discord_message = original_discord
    check(f"session alert sent with no credentials (got {captured})", captured == ["discord"])

    await runner.cleanup()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all notification checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
