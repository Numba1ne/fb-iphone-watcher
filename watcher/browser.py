"""Playwright session lifecycle with a persisted, logged-in Facebook profile."""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .models import AppConfig

log = logging.getLogger(__name__)

# Any of these in the URL or DOM after navigation means we are on a login
# wall, not on real results.
_LOGIN_URL_MARKERS = ("/login", "checkpoint", "login.php")
_LOGIN_DOM_SELECTOR = 'input[name="email"]'

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class SessionExpired(Exception):
    """The stored Facebook session is missing or no longer valid."""


class BrowserSession:
    """A persistent Chromium profile reused across every poll.

    Persisting the profile directory (rather than exporting cookies to JSON)
    keeps localStorage, device trust and the session intact, and survives
    Playwright upgrades.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._playwright = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        Path(self.cfg.user_data_dir).mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.cfg.user_data_dir,
            headless=self.cfg.headless,
            user_agent=_USER_AGENT,
            viewport={"width": 1400, "height": 1000},
            locale="en-GB",
            timezone_id="Europe/London",
            # A locale/geo mismatch with the session's cookies is a classic
            # trigger for Facebook's "something went wrong" screens.
            args=[
                "--disable-blink-features=AutomationControlled",
                # Facebook serves the search page from an edge cache. This keeps
                # the local profile from layering a second cache in front of it.
                "--disable-application-cache",
                "--disk-cache-size=1048576",
            ],
        )
        self._context.set_default_navigation_timeout(self.cfg.navigation_timeout_ms)
        log.info("Browser started (headless=%s)", self.cfg.headless)

    async def stop(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def restart(self) -> None:
        log.info("Restarting browser to reclaim memory")
        await self.stop()
        await self.start()

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("BrowserSession.start() was not called")
        page = await self._context.new_page()
        # Images and media aren't needed to parse listings; skipping them is
        # the single biggest speed-up for a text-only scrape.
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,mp4,woff,woff2}",
            lambda route: route.abort(),
        )
        # Ask every origin not to hand us a cached document. The URL nonce in
        # open_search() covers the edge cache; these cover the browser.
        await page.set_extra_http_headers(
            {
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
            }
        )
        return page

    async def assert_logged_in(self, page: Page) -> None:
        """Raise SessionExpired if this page is a login wall rather than results."""
        url = page.url.lower()
        if any(marker in url for marker in _LOGIN_URL_MARKERS):
            raise SessionExpired(f"Redirected to a login page: {page.url}")
        if await page.query_selector(_LOGIN_DOM_SELECTOR) is not None:
            raise SessionExpired("A login form is visible instead of listings")
