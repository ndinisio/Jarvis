"""Browser control.

Browsers are addressed through a small driver interface so Safari, Chrome and
Arc can each be supported without the rest of JARVIS knowing which is in use.
Interaction stays semantic (AppleScript, native `open`) rather than clicking at
screen coordinates.
"""

from __future__ import annotations

import abc
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from ..macos.tools import normalise_url
from ..web.search import search_url


class BrowserDriver(abc.ABC):
    app_name: str

    def __init__(self, controller):
        self._c = controller

    @abc.abstractmethod
    async def current_page(self) -> dict[str, str]: ...

    @abc.abstractmethod
    async def page_text(self) -> str: ...

    async def open(self, url: str) -> bool:
        return (await self._c.open_url(url, self.app_name)).ok

    async def tabs(self) -> list[dict[str, str]]:
        return []


class SafariDriver(BrowserDriver):
    app_name = "Safari"

    async def current_page(self) -> dict[str, str]:
        result = await self._c.osascript(
            'tell application "Safari" to return (URL of front document) & "\\n" & '
            "(name of front document)"
        )
        if not result.ok:
            return {}
        parts = result.stdout.strip().split("\n", 1)
        return {"url": parts[0].strip(), "title": parts[1].strip() if len(parts) > 1 else ""}

    async def page_text(self) -> str:
        result = await self._c.osascript(
            'tell application "Safari" to return (do JavaScript "document.body.innerText" '
            "in front document)",
            timeout=30.0,
        )
        return result.stdout.strip() if result.ok else ""

    async def tabs(self) -> list[dict[str, str]]:
        result = await self._c.osascript(
            'tell application "Safari" to return (URL of every tab of front window) as string'
        )
        if not result.ok:
            return []
        return [{"url": u.strip()} for u in result.stdout.split(",") if u.strip()]


class ChromiumDriver(BrowserDriver):
    """Chrome, Brave, Edge and Arc all speak the same AppleScript dialect."""

    def __init__(self, controller, app_name: str = "Google Chrome"):
        super().__init__(controller)
        self.app_name = app_name

    async def current_page(self) -> dict[str, str]:
        result = await self._c.osascript(
            f'tell application "{self.app_name}" to return (URL of active tab of front window) '
            f'& "\\n" & (title of active tab of front window)'
        )
        if not result.ok:
            return {}
        parts = result.stdout.strip().split("\n", 1)
        return {"url": parts[0].strip(), "title": parts[1].strip() if len(parts) > 1 else ""}

    async def page_text(self) -> str:
        result = await self._c.osascript(
            f'tell application "{self.app_name}" to return (execute active tab of front window '
            'javascript "document.body.innerText")',
            timeout=30.0,
        )
        return result.stdout.strip() if result.ok else ""

    async def tabs(self) -> list[dict[str, str]]:
        result = await self._c.osascript(
            f'tell application "{self.app_name}" to return (URL of every tab of front window) as string'
        )
        if not result.ok:
            return []
        return [{"url": u.strip()} for u in result.stdout.split(",") if u.strip()]


def driver_for(controller, name: str) -> BrowserDriver:
    lowered = (name or "safari").lower()
    if "safari" in lowered:
        return SafariDriver(controller)
    mapping = {
        "chrome": "Google Chrome", "brave": "Brave Browser", "edge": "Microsoft Edge",
        "arc": "Arc", "chromium": "Chromium",
    }
    for key, app in mapping.items():
        if key in lowered:
            return ChromiumDriver(controller, app)
    return SafariDriver(controller)


class OpenInBrowserTool(Tool):
    spec = ToolSpec(
        name="browse_to",
        description="Open a URL or run a web search in the browser",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "default": ""},
                "query": {"type": "string", "default": ""},
                "browser": {"type": "string", "default": ""},
            },
        },
        risk=RiskLevel.LOW,
        category="browser",
        requires_network=True,
        expected_ms=700,
        examples=["Go to apple.com", "Search the web for tide times"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args.get("url") or ""
        query = args.get("query") or ""
        if not url and not query:
            return ToolResult.failure("I need a web address or something to search for.")
        target = normalise_url(url) if url else search_url(query)
        browser = args.get("browser") or ""
        result = await self._deps.controller.open_url(target, browser or None)
        if not result.ok:
            return ToolResult.failure("The browser didn't respond.", detail=result.output)
        label = _domain(target) if url else f"a search for “{query}”"
        return ToolResult(data={"url": target}, summary=f"Opening {label}.",
                          display={"kind": "link", "url": target})


class CurrentPageTool(Tool):
    spec = ToolSpec(
        name="get_current_page",
        description="Read the URL, title and text of the page in the front browser window",
        parameters={
            "type": "object",
            "properties": {
                "browser": {"type": "string", "default": ""},
                "include_text": {"type": "boolean", "default": True},
            },
        },
        risk=RiskLevel.LOW,
        category="browser",
        requires_macos=True,
        expected_ms=1200,
        examples=["What page am I on?", "Summarise this webpage"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("browser") or await self._detect_browser()
        driver = driver_for(self._deps.controller, name)
        page = await driver.current_page()
        if not page.get("url"):
            return ToolResult.failure(
                f"{driver.app_name} didn't respond to the automation request. "
                "It may not be running, or automation permission is missing."
            )
        text = ""
        if args.get("include_text", True):
            text = await driver.page_text()
            if not text:
                # JavaScript-from-AppleScript is off by default in Safari; fall
                # back to fetching the URL directly.
                from ..web.extract import fetch_page

                fetched = await fetch_page(page["url"],
                                           user_agent=self._deps.config.research.user_agent)
                text = fetched.text
        return ToolResult(
            data={"url": page["url"], "title": page.get("title", ""), "text": text[:12000],
                  "browser": driver.app_name},
            summary=f"You're on {page.get('title') or _domain(page['url'])}.",
            display={"kind": "page", "title": page.get("title", ""), "url": page["url"],
                     "text": text[:3000]},
        )

    async def _detect_browser(self) -> str:
        front = await self._deps.controller.frontmost_app()
        if front and any(b in front.lower() for b in ("safari", "chrome", "brave", "edge", "arc")):
            return front
        return self._deps.config.capabilities and "Safari"


class BrowserTabsTool(Tool):
    spec = ToolSpec(
        name="list_browser_tabs",
        description="List the tabs open in the front browser window",
        parameters={"type": "object", "properties": {"browser": {"type": "string", "default": ""}}},
        risk=RiskLevel.LOW,
        category="browser",
        requires_macos=True,
        expected_ms=900,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = driver_for(self._deps.controller, args.get("browser") or "Safari")
        tabs = await driver.tabs()
        if not tabs:
            return ToolResult.failure(f"{driver.app_name} didn't return any tabs.")
        return ToolResult(
            data={"tabs": tabs},
            summary=f"{len(tabs)} tabs open in {driver.app_name}.",
            display={"kind": "list", "title": f"{driver.app_name} tabs",
                     "items": [t["url"] for t in tabs]},
        )


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.replace("www.", "") or url


def browser_tools(deps) -> list[Tool]:
    return [OpenInBrowserTool(deps), CurrentPageTool(deps), BrowserTabsTool(deps)]
