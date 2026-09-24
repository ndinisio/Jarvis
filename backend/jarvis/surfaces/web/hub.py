"""Which browser a web action goes to.

JARVIS works in two browsers, each where it is best (``BrowserConfig``):

* **Your everyday browser** (Safari, Chrome, Arc… over AppleScript) for the
  page you are looking at — "what's on this page", "fill in this form" — and
  for quick one-off opens, where your own logins and tabs are the point.
* **JARVIS Chrome** (its own Chrome profile over the DevTools protocol) for
  errands: multi-step work that navigates somewhere. It waits for pages
  properly, types and clicks with genuine input, and never disturbs your
  tabs.

The rule, applied per task:

1. A browser named in the request wins ("in Safari…").
2. A task keeps the browser it started in — every step of one errand
   happens in one window.
3. A task whose first web step *navigates* is an errand: JARVIS Chrome
   (unless ``site_overrides`` sends that site to the everyday browser, or
   JARVIS Chrome is off or unavailable). A task whose first web step *reads
   or acts on the current page* means your page: the everyday browser.
4. Anything outside a task — a quick "open YouTube" — is the everyday
   browser.

If JARVIS Chrome can't start (Playwright not installed, no Chrome and no
bundled Chromium), everything falls back to the everyday browser. Off macOS
there is no everyday browser to reach, so only JARVIS Chrome is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

from ...core.logging import get_logger

log = get_logger("jarvis.surfaces.web.hub")

IS_MACOS = platform.system() == "Darwin"

#: Words that name JARVIS's own browser in a tool call's ``browser`` argument.
_JARVIS_NAMES = ("jarvis", "own browser", "automation")

#: How many tasks' browser choices to remember.
_MAX_BOUND = 32


class BrowserHub:
    """Picks, starts and remembers the browser each web action uses."""

    def __init__(self, deps):
        self._deps = deps
        self._jarvis_browser = None
        self._jarvis_driver = None
        self._jarvis_error = ""
        self._start_lock = asyncio.Lock()
        self._pinned = None
        self._bound: OrderedDict[str, Any] = OrderedDict()
        self._last_everyday = ""

    # -- configuration ----------------------------------------------------------
    @property
    def _config(self):
        return self._deps.config.browser

    def choose(self, url: str = "") -> str:
        """``"jarvis"`` or ``"everyday"`` for an errand that starts at *url*."""
        conf = self._config
        host = (urlparse(url).hostname or "").lower()
        if host:
            for site, where in conf.site_overrides.items():
                site = site.lower().lstrip(".")
                if host == site or host.endswith("." + site):
                    return "everyday" if str(where).lower().startswith("every") else "jarvis"
        return "jarvis" if conf.jarvis_browser else "everyday"

    @property
    def jarvis_unavailable(self) -> str:
        """Why JARVIS Chrome couldn't start, if it couldn't."""
        return self._jarvis_error

    # -- evaluation / tests ------------------------------------------------------
    @contextlib.contextmanager
    def pin(self, driver):
        """Send every web action to *driver* for the duration (evaluation runs
        and tests, which bring their own browser)."""
        saved = self._pinned
        self._pinned = driver
        try:
            yield driver
        finally:
            self._pinned = saved

    # -- choosing ----------------------------------------------------------------
    async def for_action(self, ctx=None, *, navigating: bool = False, url: str = "",
                         browser: str = ""):
        """The driver a web action should use, or ``None`` if no browser is
        reachable at all."""
        if self._pinned is not None:
            return self._pinned
        task_id = getattr(ctx, "task_id", None)
        if browser:
            driver = await self._named(browser)
            if driver is not None:
                self._bind(task_id, driver)
                return driver
        if task_id and task_id in self._bound:
            self._bound.move_to_end(task_id)
            return self._bound[task_id]
        driver = None
        if task_id and navigating and self.choose(url) == "jarvis":
            driver = await self.jarvis()
        if driver is None:
            driver = await self.everyday()
            if driver is None:
                # Off macOS (or with no everyday browser to reach), JARVIS
                # Chrome is the only browser there is.
                driver = await self.jarvis()
        self._bind(task_id, driver)
        return driver

    def _bind(self, task_id: str | None, driver) -> None:
        if not task_id or driver is None:
            return
        self._bound[task_id] = driver
        self._bound.move_to_end(task_id)
        while len(self._bound) > _MAX_BOUND:
            self._bound.popitem(last=False)

    def bound(self, task_id: str | None):
        """The browser a task is working in, if it has one."""
        return self._bound.get(task_id) if task_id else None

    def release(self, task_id: str | None) -> None:
        if task_id:
            self._bound.pop(task_id, None)

    async def _named(self, name: str):
        lowered = name.lower()
        if any(word in lowered for word in _JARVIS_NAMES):
            return await self.jarvis()
        return await self.everyday(name)

    # -- the two browsers ----------------------------------------------------------
    async def everyday(self, name: str = ""):
        """The user's own browser, over AppleScript — ``None`` off macOS."""
        if not IS_MACOS:
            return None
        # Looked up on the module so a test can substitute either function.
        from ...tools.browser import tools as browser_tools

        if not name:
            name = await browser_tools.detect_browser(self._deps)
            if name == "Safari" and self._last_everyday:
                # Nothing browser-like is in front; the browser JARVIS last
                # used is a better guess than a hard-coded one.
                name = self._last_everyday
        self._last_everyday = name
        return browser_tools.driver_for(self._deps.controller, name)

    async def jarvis(self):
        """JARVIS Chrome, started on first use — ``None`` if it can't run."""
        conf = self._config
        if not conf.jarvis_browser:
            return None
        # Once started, the same driver serves for good: if the user closes
        # the window, its browser opens a fresh one on the next action.
        if self._jarvis_driver is not None:
            return self._jarvis_driver
        async with self._start_lock:
            if self._jarvis_driver is not None:
                return self._jarvis_driver
            if self._jarvis_error:
                return None
            from .cdp import PlaywrightBrowser, PlaywrightDriver

            if not PlaywrightBrowser.installed():
                self._jarvis_error = 'Playwright isn\'t installed (pip install -e ".[browser]")'
                log.info("JARVIS Chrome unavailable: %s", self._jarvis_error)
                return None
            browser = await self._launch(PlaywrightBrowser, conf)
            if browser is None:
                return None
            self._jarvis_browser = browser
            self._jarvis_driver = PlaywrightDriver(browser)
            return self._jarvis_driver

    async def _launch(self, browser_cls, conf):
        attempts = [conf.channel or None]
        if conf.channel:
            attempts.append(None)  # Chrome isn't installed: Playwright's own Chromium.
        last = ""
        for channel in attempts:
            browser = browser_cls(user_data_dir=conf.profile_dir, channel=channel,
                                  headless=conf.headless)
            try:
                await browser.start()
                log.info("JARVIS Chrome started (%s)", channel or "bundled Chromium")
                return browser
            except Exception as exc:
                last = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                log.debug("JARVIS Chrome didn't start with channel=%s: %s", channel, exc)
                with contextlib.suppress(Exception):
                    await browser.close()
        self._jarvis_error = f"JARVIS Chrome didn't start: {last}"
        log.info(self._jarvis_error)
        return None

    async def picture(self, task_id: str | None = None, *, scale: float = 0.5,
                      quality: int = 60) -> bytes | None:
        """A small picture of the page a task (or JARVIS generally) is working
        on — only from a browser JARVIS drives itself, and never by starting
        one. ``None`` when there's nothing to show."""
        driver = self._pinned or (self._bound.get(task_id) if task_id else None) or self._jarvis_driver
        take = getattr(driver, "picture", None)
        if take is None:
            return None
        return await take(scale=scale, quality=quality)

    async def close(self) -> None:
        if self._jarvis_browser is not None:
            with contextlib.suppress(Exception):
                await self._jarvis_browser.close()
        self._jarvis_browser = self._jarvis_driver = None
        self._bound.clear()


def hub_of(deps) -> BrowserHub:
    """The app's hub — or, for a bare dependency container (tests), one made
    on first use and kept on it."""
    hub = getattr(deps, "browsers", None)
    if hub is None:
        hub = BrowserHub(deps)
        with contextlib.suppress(Exception):
            deps.browsers = hub
    return hub
