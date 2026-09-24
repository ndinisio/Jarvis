"""Browser control.

Browsers are addressed through a small driver interface so Safari, Chrome and
Arc can each be supported without the rest of JARVIS knowing which is in use.
Interaction stays semantic (AppleScript, native `open`) rather than clicking at
screen coordinates.
"""

from __future__ import annotations

import abc
import json
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from ..macos.tools import normalise_url
from ..web.search import search_url
from . import manifest_js

#: Keys a page action may press, as the model names them → the standard
#: ``KeyboardEvent.key`` value (which is also what Playwright expects).
PAGE_KEYS = {
    "enter": "Enter", "return": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End",
    "backspace": "Backspace", "delete": "Delete", "space": " ",
}


def normalise_key(key: str) -> str:
    """``"page down"`` → ``"PageDown"``; ``""`` for a key pages aren't sent."""
    compact = "".join(str(key or "").lower().split()).replace("_", "").replace("-", "")
    return PAGE_KEYS.get(compact, "")


class BrowserDriver(abc.ABC):
    app_name: str
    #: Whether JARVIS runs this browser itself (JARVIS Chrome) rather than
    #: reaching into the user's own over AppleScript.
    owned = False

    def __init__(self, controller):
        self._c = controller

    @abc.abstractmethod
    async def current_page(self) -> dict[str, str]: ...

    @abc.abstractmethod
    async def run_js(self, script: str, *, timeout: float = 20.0) -> str:
        """Execute *script* in the front tab/document, return its result as
        a string. Every write-capable web primitive below is built on this
        one per-browser AppleScript↔JS bridge — the same mechanism the
        (read-only) :meth:`page_text` already used."""

    async def open(self, url: str) -> bool:
        return (await self._c.open_url(url, self.app_name)).ok

    async def tabs(self) -> list[dict[str, str]]:
        return []

    async def page_text(self) -> str:
        return await self.run_js("document.body.innerText", timeout=30.0)

    async def can_execute_js(self) -> bool:
        """Cheap probe for the developer setting every write primitive here
        depends on ("Allow JavaScript from Apple Events"), which is off by
        default in both Safari and Chromium-family browsers."""
        return (await self.run_js("1+1")).strip() == "2"

    # -- grounded interaction, built entirely on run_js() -------------------
    async def page_manifest(self, *, limit: int = 60, roles: list[str] | None = None,
                            offset: int = 0) -> dict[str, Any]:
        raw = await self.run_js(manifest_js.build_manifest_script(limit=limit, roles=roles,
                                                                   offset=offset),
                                timeout=20.0)
        return _parse_js_json(raw)

    async def inspect_handle(self, handle: str) -> dict[str, Any]:
        return _parse_js_json(await self.run_js(manifest_js.build_inspect_script(handle), timeout=10.0))

    async def click_handle(self, handle: str) -> dict[str, Any]:
        return _parse_js_json(await self.run_js(manifest_js.build_click_script(handle)))

    async def fill_handle(self, handle: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        raw = await self.run_js(manifest_js.build_fill_script(handle, text, submit))
        return _parse_js_json(raw)

    async def submit_handle(self, handle: str) -> dict[str, Any]:
        return _parse_js_json(await self.run_js(manifest_js.build_submit_script(handle)))

    async def scroll(self, direction: str = "down", handle: str = "") -> dict[str, Any]:
        return _parse_js_json(await self.run_js(manifest_js.build_scroll_script(direction, handle)))

    async def go_back(self) -> dict[str, Any]:
        return _parse_js_json(await self.run_js(manifest_js.back_script()))

    async def press_key(self, key: str, handle: str = "") -> dict[str, Any]:
        return _parse_js_json(await self.run_js(manifest_js.build_key_script(key, handle)))

    async def focused_handle(self) -> str:
        """A handle for the element with keyboard focus, ``""`` if none."""
        return str(_parse_js_json(await self.run_js(manifest_js.active_element_script(),
                                                    timeout=10.0)).get("handle") or "")

    async def has_text(self, text: str) -> bool:
        return bool(_parse_js_json(await self.run_js(manifest_js.build_find_text_script(text),
                                                     timeout=10.0)).get("found"))

    async def bring_to_front(self) -> None:
        """Show this browser's window — so the user can take over in it."""
        if self._c is not None:
            await self._c.osascript(f'tell application "{self.app_name}" to activate')


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

    async def run_js(self, script: str, *, timeout: float = 20.0) -> str:
        result = await self._c.osascript(
            'tell application "Safari" to return (do JavaScript "'
            + _as_applescript_literal(script) + '" in front document)',
            timeout=timeout,
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

    async def run_js(self, script: str, *, timeout: float = 20.0) -> str:
        result = await self._c.osascript(
            f'tell application "{self.app_name}" to return (execute active tab of front window '
            'javascript "' + _as_applescript_literal(script) + '")',
            timeout=timeout,
        )
        return result.stdout.strip() if result.ok else ""

    async def tabs(self) -> list[dict[str, str]]:
        result = await self._c.osascript(
            f'tell application "{self.app_name}" to return (URL of every tab of front window) as string'
        )
        if not result.ok:
            return []
        return [{"url": u.strip()} for u in result.stdout.split(",") if u.strip()]


def _as_applescript_literal(script: str) -> str:
    """Escape JS *script* for embedding as an AppleScript double-quoted
    string literal — the same escaping ``macos/controller.py: _esc()`` uses
    for any AppleScript literal, applied here to a whole JS program rather
    than a single value. Every dynamic value inside *script* was already
    embedded via ``json.dumps()`` (see ``manifest_js.py``), so this layer
    only ever has to protect the literal boundary itself, not page content."""
    return script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _parse_js_json(raw: str) -> dict[str, Any]:
    """Every script in ``manifest_js.py`` returns ``JSON.stringify(...)`` of
    its result — a plain JS string, which is what survives the AppleScript
    round trip losslessly. Parse it back, and fail safely (never raise) when
    the browser returned nothing usable, which is exactly what happens when
    JavaScript-from-Apple-Events is off."""
    if not raw:
        return {"ok": False, "reason": "the browser didn't respond \u2014 JavaScript from Apple "
                                       "Events may be disabled, or nothing is open"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "the page returned something unexpected"}
    return data if isinstance(data, dict) else {"ok": False, "reason": "unexpected response shape"}


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


async def navigate(deps, ctx: ToolContext | None, target: str, browser: str = "", *,
                   label: str = "") -> ToolResult:
    """Open *target* in whichever browser the hub picks for this call."""
    from ...surfaces.web.hub import hub_of
    from .observe import acted

    label = label or _domain(target)
    acted()                                   # whichever page this lands in
    driver = await hub_of(deps).for_action(ctx, navigating=True, url=target, browser=browser)
    if driver is not None and driver.owned:
        # JARVIS Chrome: the call returns once the page has loaded.
        if not await driver.open(target):
            return ToolResult.failure(f"{label[:1].upper() + label[1:]} didn't load in {driver.app_name}.")
        page = await driver.current_page()
        return ToolResult(data={"url": page.get("url") or target, "browser": driver.app_name},
                          summary=f"Opened {label}.",
                          observation=f"Opened {page.get('title') or label} — {page.get('url') or target}",
                          display={"kind": "link", "url": target})
    # The everyday browser: the system opens it, exactly as a link would.
    result = await deps.controller.open_url(target, browser or None)
    if not result.ok:
        return ToolResult.failure("The browser didn't respond.", detail=result.output)
    return ToolResult(data={"url": target}, summary=f"Opening {label}.",
                      display={"kind": "link", "url": target})


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
        label = _domain(target) if url else f"a search for “{query}”"
        return await navigate(self._deps, ctx, target, browser, label=label)


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
        expected_ms=1200,
        examples=["What page am I on?", "Summarise this webpage"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ...surfaces.web.hub import hub_of

        driver = await hub_of(self._deps).for_action(ctx, browser=args.get("browser") or "")
        if driver is None:
            return ToolResult.failure("No browser is reachable from here.")
        page = await driver.current_page()
        if not page.get("url"):
            return ToolResult.failure(
                f"{driver.app_name} didn't respond to the automation request. "
                "It may not be running, or automation permission is missing."
            )
        text = ""
        if args.get("include_text", True):
            from .observe import settle

            await settle(driver)          # loaded, fetched and still — at once if it already is
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
            observation=(f"Page: {page.get('title') or 'untitled'} — {page['url']}\n"
                         + (f"Page text: {' '.join(text.split())[:3000]}" if text else "")),
            display={"kind": "page", "title": page.get("title", ""), "url": page["url"],
                     "text": text[:3000]},
        )


class BrowserTabsTool(Tool):
    spec = ToolSpec(
        name="list_browser_tabs",
        description="List the tabs open in the front browser window",
        parameters={"type": "object", "properties": {"browser": {"type": "string", "default": ""}}},
        risk=RiskLevel.LOW,
        category="browser",
        expected_ms=900,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ...surfaces.web.hub import hub_of

        driver = await hub_of(self._deps).for_action(ctx, browser=args.get("browser") or "")
        if driver is None:
            return ToolResult.failure("No browser is reachable from here.")
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


async def detect_browser(deps) -> str:
    """Which browser to address when a tool call doesn't name one — the
    frontmost app if it looks like a browser, Safari otherwise. Shared by
    every browser/page tool so they all guess the same way."""
    front = await deps.controller.frontmost_app()
    if front and any(b in front.lower() for b in ("safari", "chrome", "brave", "edge", "arc")):
        return front
    return "Safari"


def browser_tools(deps) -> list[Tool]:
    return [OpenInBrowserTool(deps), CurrentPageTool(deps), BrowserTabsTool(deps)]
