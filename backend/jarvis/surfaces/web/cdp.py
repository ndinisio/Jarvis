"""JARVIS's own browser, driven over the Chrome DevTools protocol (via Playwright).

Unlike the everyday-browser drivers in ``tools/browser/tools.py``, which reach
Safari or Chrome through AppleScript and can only inject JavaScript, this is a
browser JARVIS launches and owns: it can wait for a page to finish loading,
deliver genuine input events, and take screenshots of a single tab. The
profile lives in its own directory, so logins made in it persist between runs
without touching the user's everyday browser profile (Chrome refuses DevTools
control of its default profile anyway).

Playwright is an optional dependency (``pip install -e ".[browser]"``);
nothing here is imported unless this browser is actually used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from ...core.logging import get_logger
from ...tools.browser.tools import BrowserDriver

log = get_logger("jarvis.surfaces.web.cdp")

#: Errors Playwright raises while a navigation replaces the page's JS context.
_NAVIGATION_ERRORS = ("Execution context was destroyed", "navigation", "Target closed",
                      "frame was detached", "Cannot find context")


class PlaywrightBrowser:
    """Owns one Playwright browser context and its pages."""

    def __init__(self, *, user_data_dir: str | Path | None = None, channel: str | None = None,
                 headless: bool = False, executable_path: str | None = None,
                 viewport: tuple[int, int] = (1280, 900)):
        self.user_data_dir = Path(user_data_dir).expanduser() if user_data_dir else None
        self.channel = channel
        self.headless = headless
        self.executable_path = executable_path
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self._playwright = None
        self._browser = None
        self.context = None
        self._page = None
        self._lock = asyncio.Lock()

    @staticmethod
    def installed() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    async def start(self) -> PlaywrightBrowser:
        async with self._lock:
            if self.context is not None:
                return self
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            chromium = self._playwright.chromium
            options: dict[str, Any] = {"headless": self.headless}
            if self.channel:
                options["channel"] = self.channel
            if self.executable_path:
                options["executable_path"] = self.executable_path
            if self.user_data_dir is not None:
                self.user_data_dir.mkdir(parents=True, exist_ok=True)
                self.context = await chromium.launch_persistent_context(
                    str(self.user_data_dir), viewport=self.viewport, **options)
            else:
                self._browser = await chromium.launch(**options)
                self.context = await self._browser.new_context(viewport=self.viewport)
            self.context.on("page", self._adopt)
            pages = self.context.pages
            self._page = pages[0] if pages else await self.context.new_page()
            return self

    def _adopt(self, page) -> None:
        """A new tab or popup becomes the one JARVIS is looking at — the same
        thing a person's attention does when a link opens a new tab."""
        self._page = page

    @property
    def page(self):
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in (self.context.pages if self.context else []) if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else None
        return self._page

    async def ensure_page(self):
        await self.start()
        page = self.page
        if page is None:
            page = await self.context.new_page()
            self._page = page
        return page

    async def close(self) -> None:
        async with self._lock:
            with contextlib.suppress(Exception):
                if self.context is not None:
                    await self.context.close()
            with contextlib.suppress(Exception):
                if self._browser is not None:
                    await self._browser.close()
            with contextlib.suppress(Exception):
                if self._playwright is not None:
                    await self._playwright.stop()
            self.context = self._browser = self._playwright = self._page = None


class PlaywrightDriver(BrowserDriver):
    """The :class:`BrowserDriver` interface over a :class:`PlaywrightBrowser`.

    Every write primitive the base class builds on ``run_js`` works unchanged;
    what this adds is that navigation actually waits for the page, and that
    a script racing a navigation is retried against the new page instead of
    silently returning nothing.
    """

    app_name = "JARVIS Chrome"

    def __init__(self, browser: PlaywrightBrowser, *, navigation_timeout_s: float = 20.0):
        super().__init__(controller=None)
        self.browser = browser
        self.navigation_timeout_s = navigation_timeout_s

    async def current_page(self) -> dict[str, str]:
        page = await self.browser.ensure_page()
        with contextlib.suppress(Exception):
            return {"url": page.url, "title": await page.title()}
        return {"url": page.url, "title": ""}

    async def run_js(self, script: str, *, timeout: float = 20.0) -> str:
        for attempt in range(3):
            page = await self.browser.ensure_page()
            try:
                value = await asyncio.wait_for(page.evaluate(script), timeout=timeout)
            except asyncio.TimeoutError:
                log.debug("script timed out after %.1fs", timeout)
                return ""
            except Exception as exc:
                if attempt < 2 and any(marker in str(exc) for marker in _NAVIGATION_ERRORS):
                    await self.settle()
                    continue
                log.debug("script failed: %s", exc)
                return ""
            if value is None:
                return ""
            return value if isinstance(value, str) else json.dumps(value)
        return ""

    async def open(self, url: str) -> bool:
        for attempt in range(2):
            page = await self.browser.ensure_page()
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=self.navigation_timeout_s * 1000)
            except Exception as exc:
                # A navigation still settling from the previous action can
                # interrupt this one; that is worth exactly one retry.
                if attempt == 0 and "interrupted by another navigation" in str(exc):
                    await self.settle()
                    continue
                log.debug("navigation to %s failed: %s", url, exc)
                return False
            await self.settle()
            return True
        return False

    async def settle(self, timeout_s: float = 5.0) -> None:
        """Wait until the page has stopped loading — bounded, never fatal."""
        page = self.browser.page
        if page is None:
            return
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_s * 1000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=min(timeout_s, 2.5) * 1000)

    async def tabs(self) -> list[dict[str, str]]:
        await self.browser.start()
        tabs = []
        for page in self.browser.context.pages:
            if page.is_closed():
                continue
            title = ""
            with contextlib.suppress(Exception):
                title = await page.title()
            tabs.append({"url": page.url, "title": title})
        return tabs

    async def can_execute_js(self) -> bool:
        return (await self.run_js("1+1")).strip() == "2"

    # Actions can trigger navigation after the script returns; wait for it so
    # the next read sees the destination rather than the page being left.
    async def click_handle(self, handle: str) -> dict[str, Any]:
        result = await super().click_handle(handle)
        await self.settle()
        return result

    async def fill_handle(self, handle: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        result = await super().fill_handle(handle, text, submit=submit)
        await self.settle()
        return result

    async def submit_handle(self, handle: str) -> dict[str, Any]:
        result = await super().submit_handle(handle)
        await self.settle()
        return result
