"""Telegram and Discord delivery, fired in parallel with backoff retries."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

import aiohttp

from .models import AppConfig, Listing

log = logging.getLogger(__name__)

_TELEGRAM_MAX = 4096


class Notifier:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "Notifier":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()

    # ---------------------------------------------------------------- public

    async def send(self, listing: Listing) -> bool:
        """Send one listing to every enabled channel. True if any succeeded."""
        tasks = []
        if self.cfg.telegram:
            tasks.append(self._telegram_message(_telegram_text(listing)))
        if self.cfg.discord:
            tasks.append(self._discord_listing(listing))

        if not tasks:
            # No channel has credentials, so there is nothing to send to. Say
            # so once per listing rather than failing silently in a way that
            # looks like a broken watcher.
            log.warning("No delivery channel configured — %s not sent", listing.title)
            return False

        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = False
        for result in results:
            if isinstance(result, Exception):
                log.error("Notification failed: %s", result)
            elif result:
                ok = True
        return ok

    async def send_test(self) -> bool:
        """Send a sample alert so the user can confirm wiring end-to-end."""
        sample = Listing.from_card(
            item_id="000000000",
            title="TEST — iPhone 13 128GB, Blue",
            price=280,
            location="Croydon, London",
        )
        return await self.send(sample)

    async def alert_session_expired(self, detail: str) -> None:
        text = (
            "⚠️ <b>Facebook session expired</b>\n"
            f"{html.escape(detail)}\n"
            "Run <code>python main.py --login</code> and log in again."
        )
        tasks = []
        if self.cfg.telegram:
            tasks.append(self._telegram_message(text))
        if self.cfg.discord:
            tasks.append(self._discord_message(text))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------- internals

    async def _post(self, url: str, payload: dict, *, label: str) -> bool:
        assert self._session is not None
        delay = 1.0
        for attempt in range(1, 4):
            try:
                async with self._session.post(url, json=payload) as resp:
                    if resp.status < 300:
                        return True
                    body = (await resp.text())[:300]
                    if resp.status < 500:
                        # 4xx: bad token or webhook URL. Retrying won't help.
                        log.error(
                            "%s rejected the request (%s): %s", label, resp.status, body
                        )
                        return False
                    log.warning(
                        "%s returned %s (attempt %d/3)", label, resp.status, attempt
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning(
                    "%s network error (attempt %d/3): %s", label, attempt, exc
                )
            except Exception as exc:  # noqa: BLE001 - never let alerting crash the loop
                log.error("%s unexpected error: %s", label, exc)
                return False

            if attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2
        log.error("%s failed after 3 attempts", label)
        return False

    async def _telegram_message(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text[:_TELEGRAM_MAX],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        return await self._post(url, payload, label="Telegram")

    async def _discord_message(self, text: str) -> bool:
        # Discord has no HTML, so strip the handful of tags we emit. This must
        # not depend on Telegram being enabled: a Discord-only setup would
        # otherwise show literal <b> and <code> in the alert.
        for tag in ("<b>", "</b>", "<code>", "</code>"):
            text = text.replace(tag, "")
        return await self._post(
            self.cfg.discord_webhook_url, {"content": text}, label="Discord"
        )

    async def _discord_listing(self, listing: Listing) -> bool:
        colour = 0x2ECC71 if (listing.price or 0) < 400 else 0xE67E22
        now = datetime.now().strftime("%H:%M:%S")
        payload = {
            "embeds": [
                {
                    "title": listing.title[:256],
                    "url": listing.url,
                    "description": f"**{listing.price_text}**",
                    "color": colour,
                    "fields": [
                        {"name": "Location", "value": listing.location, "inline": True},
                        {
                            "name": "Listed",
                            "value": listing.listed_display,
                            "inline": True,
                        },
                    ],
                    "footer": {"text": f"Seen at {now}"},
                }
            ]
        }
        return await self._post(
            self.cfg.discord_webhook_url, payload, label="Discord"
        )


def _telegram_text(listing: Listing) -> str:
    return (
        f"🚨 <b>New iPhone listing — {html.escape(listing.price_text)}</b>\n"
        f"📱 {html.escape(listing.title)}\n"
        f"📍 {html.escape(listing.location)}\n"
        f"🕒 Listed {html.escape(listing.listed_display)}\n"
        f"🔗 {listing.url}\n"
        f"<i>seen {datetime.now():%H:%M:%S}</i>"
    )
