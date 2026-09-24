"""JARVIS's own browser, driven over the Chrome DevTools protocol (via Playwright).

Unlike the everyday-browser drivers in ``tools/browser/tools.py``, which reach
Safari or Chrome through AppleScript and can only inject JavaScript, this is a
browser JARVIS launches and owns: it can wait for a page to finish loading,
deliver genuine input events, and take screenshots of a single tab. The
profile lives in its own directory, so logins made in it persist between runs
without touching the user's everyday browser profile (Chrome refuses DevTools
control of its default profile anyway).

**Genuine input (v3.0).** Clicks and typing go through Playwright's input
pipeline — real mouse events at the element's position and real keystrokes
— so autocompletes, React inputs and "is a person there?" handlers respond
as they would to a person. The element is still addressed by the handle the
page listing stamped on it, found in whichever frame or shadow root it
lives in. If the genuine action can't be delivered (the element is covered,
or never becomes clickable), the in-page script is the fallback.

**Settling.** After an action the page is loading, fetching, re-rendering,
or all three. :meth:`PlaywrightDriver.settle` waits for the load, then for
the page's own requests to finish — a "Send" whose ``fetch`` is still in
flight hasn't happened yet — bounded, never fatal.

Playwright is an optional dependency (``pip install -e ".[browser]"``);
nothing here is imported unless this browser is actually used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ...core.logging import get_logger
from ...tools.browser import sensitive
from ...tools.browser.tools import BrowserDriver, normalise_key

log = get_logger("jarvis.surfaces.web.cdp")

#: Errors Playwright raises while a navigation replaces the page's JS context.
_NAVIGATION_ERRORS = ("Execution context was destroyed", "navigation", "Target closed",
                      "frame was detached", "Cannot find context")

#: A handle as the page listing stamps it; anything else is never put in a selector.
_HANDLE = re.compile(r"^jv\d+$")

#: Requests that never "finish" in a way that matters for settling.
_LONG_LIVED = ("websocket", "eventsource")


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
        #: Requests the pages have started and not yet finished.
        self._inflight: set[Any] = set()

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

            if self._playwright is None:
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
                if self._browser is None or not self._browser.is_connected():
                    self._browser = await chromium.launch(**options)
                self.context = await self._browser.new_context(viewport=self.viewport)
            self.context.on("page", self._adopt)
            self.context.on("close", self._closed)
            self.context.on("request", self._request_started)
            self.context.on("requestfinished", self._request_done)
            self.context.on("requestfailed", self._request_done)
            pages = self.context.pages
            self._page = pages[0] if pages else await self.context.new_page()
            return self

    def _closed(self, *_args) -> None:
        # The user closed the JARVIS window (or the browser went away): the
        # next use starts a fresh one instead of talking to a dead context.
        self.context = None
        self._page = None
        self._inflight.clear()

    def _request_started(self, request) -> None:
        if request.resource_type not in _LONG_LIVED:
            self._inflight.add(request)

    def _request_done(self, request) -> None:
        self._inflight.discard(request)

    async def network_quiet(self, *, quiet_s: float = 0.25, timeout_s: float = 4.0) -> bool:
        """Wait (bounded) until no request has been in flight for *quiet_s*."""
        deadline = time.monotonic() + timeout_s
        quiet_since = None
        while time.monotonic() < deadline:
            if not self._inflight:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= quiet_s:
                    return True
            else:
                quiet_since = None
            await asyncio.sleep(0.05)
        return False

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
            self._inflight.clear()


class PlaywrightDriver(BrowserDriver):
    """The :class:`BrowserDriver` interface over a :class:`PlaywrightBrowser`.

    Reading (the page listing, inspection) runs the same in-page scripts as
    every other browser. Acting uses genuine input where it can — see the
    module docstring — and every action waits for the page to settle before
    it reports back, so the next look sees the result rather than the page
    being left.
    """

    app_name = "JARVIS Chrome"
    owned = True

    def __init__(self, browser: PlaywrightBrowser, *, navigation_timeout_s: float = 20.0,
                 action_timeout_s: float = 3.0):
        super().__init__(controller=None)
        self.browser = browser
        self.navigation_timeout_s = navigation_timeout_s
        self.action_timeout_s = action_timeout_s

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
        """Wait until the page has loaded and its requests have finished —
        bounded, never fatal."""
        page = self.browser.page
        if page is None:
            return
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_s * 1000)
        with contextlib.suppress(Exception):
            await self.browser.network_quiet(timeout_s=min(timeout_s, 4.0))

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

    async def bring_to_front(self) -> None:
        with contextlib.suppress(Exception):
            page = await self.browser.ensure_page()
            await page.bring_to_front()

    # -- genuine input ----------------------------------------------------------
    async def _locate(self, handle: str):
        """The element behind *handle*, in whichever frame it lives in.
        Playwright's CSS engine already looks inside open shadow roots."""
        if not _HANDLE.match(str(handle)):
            return None
        page = await self.browser.ensure_page()
        selector = f'[data-jarvis-id="{handle}"]'
        for frame in page.frames:
            with contextlib.suppress(Exception):
                locator = frame.locator(selector)
                if await locator.count():
                    return locator.first
        return None

    async def _outcome(self, info: dict[str, Any], **extra: Any) -> dict[str, Any]:
        await self.settle()
        page = await self.current_page()
        return {"ok": True, "text": info.get("text", ""), "url": page.get("url", ""),
                "title": page.get("title", ""), **extra}

    async def click_handle(self, handle: str) -> dict[str, Any]:
        info = await self.inspect_handle(handle)
        locator = await self._locate(handle) if info.get("found") else None
        if locator is not None:
            try:
                await locator.click(timeout=self.action_timeout_s * 1000)
                return await self._outcome(info, input="genuine")
            except Exception as exc:
                log.debug("genuine click on %s failed, using the page script: %s", handle, exc)
        result = await super().click_handle(handle)
        await self.settle()
        return result

    async def fill_handle(self, handle: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        info = await self.inspect_handle(handle)
        if not info.get("found"):
            return {"ok": False, "reason": "stale handle — the page has changed since it was read"}
        refused = sensitive.refusal(info)
        if refused:
            return {"ok": False, "refused": True, "reason": refused}
        locator = await self._locate(handle)
        if locator is None or info.get("tag") == "select":
            # Choosing an option: the in-page script matches the option the
            # way a person would describe it ("blue" for "Blue — £12.99").
            result = await super().fill_handle(handle, text, submit=submit)
            await self.settle()
            return result
        timeout = self.action_timeout_s * 1000
        try:
            if info.get("suggests"):
                # A field that offers suggestions as you type listens to
                # keystrokes, not just the final value: type it like a person.
                await locator.fill("", timeout=timeout)
                await locator.press_sequentially(str(text), delay=15, timeout=timeout * 4)
            else:
                await locator.fill(str(text), timeout=timeout)
        except Exception as exc:
            log.debug("genuine typing into %s failed, using the page script: %s", handle, exc)
            result = await super().fill_handle(handle, text, submit=submit)
            await self.settle()
            return result
        submitted = False
        if submit:
            if info.get("tag") == "textarea":
                await super().submit_handle(handle)
            else:
                with contextlib.suppress(Exception):
                    await locator.press("Enter", timeout=timeout)
            submitted = True
        return await self._outcome(info, submitted=submitted, input="genuine")

    async def submit_handle(self, handle: str) -> dict[str, Any]:
        result = await super().submit_handle(handle)
        await self.settle()
        return result

    async def press_key(self, key: str, handle: str = "") -> dict[str, Any]:
        name = normalise_key(key)
        if not name:
            return {"ok": False, "reason": f"“{key}” isn't a key JARVIS presses on web pages"}
        name = "Space" if name == " " else name
        try:
            if handle:
                locator = await self._locate(handle)
                if locator is None:
                    return {"ok": False, "reason": "stale handle — the page has changed since it was read"}
                await locator.press(name, timeout=self.action_timeout_s * 1000)
            else:
                page = await self.browser.ensure_page()
                await page.keyboard.press(name)
        except Exception as exc:
            return {"ok": False, "reason": f"the key press didn't go through ({exc})"}
        await self.settle()
        return {"ok": True}

    async def go_back(self) -> dict[str, Any]:
        page = await self.browser.ensure_page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=self.navigation_timeout_s * 1000)
        except Exception as exc:
            return {"ok": False, "reason": f"couldn't go back ({exc})"}
        await self.settle()
        return {"ok": True}
