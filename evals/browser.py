"""The evaluation browser: JARVIS's own Playwright browser, pointed at the mock sites.

Every request to a hostname in ``mock_sites/hosts.py`` is answered by the
mock server, with the URL left untouched, so the page believes it is on
``www.amazon.co.uk``. Any other host is blocked, which keeps evaluation
runs hermetic (and fast) without the internet.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from jarvis.surfaces.web.cdp import PlaywrightBrowser, PlaywrightDriver

from .mock_sites.app import MOCK_HEADER
from .mock_sites.hosts import mock_url


def _bundled_chromium() -> str | None:
    """The container ships Chromium outside Playwright's own version pin;
    use it directly when Playwright's matching build isn't installed."""
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    candidates = [root / "chromium", *sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True)]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


async def open_browser(server_url: str, *, headless: bool = True,
                       channel: str | None = None) -> PlaywrightBrowser:
    browser = PlaywrightBrowser(headless=headless, channel=channel,
                                executable_path=None if channel else _bundled_chromium())
    try:
        await browser.start()
    except Exception:
        if browser.executable_path is None:
            raise
        # A stale bundled path: fall back to whatever Playwright itself knows.
        browser.executable_path = None
        await browser.start()

    async def serve(route) -> None:
        request = route.request
        if request.url.startswith(server_url):
            await route.continue_()
            return
        target = mock_url(request.url, server_url)
        if target is None:
            # Answered rather than aborted: an aborted navigation leaves
            # Chrome's own error page racing whatever loads next.
            await route.fulfill(status=502, content_type="text/html",
                                body="<title>Blocked</title><h1>Blocked by the evaluation browser</h1>")
            return
        headers = {**request.headers, MOCK_HEADER: "1"}
        try:
            response = await route.fetch(url=target, headers=headers, max_redirects=0)
        except Exception:
            with contextlib.suppress(Exception):
                await route.abort()
            return
        await route.fulfill(response=response)

    await browser.context.route("**/*", serve)
    return browser


def driver_for(browser: PlaywrightBrowser) -> PlaywrightDriver:
    return PlaywrightDriver(browser, navigation_timeout_s=10.0)
