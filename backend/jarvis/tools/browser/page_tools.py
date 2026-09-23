"""Grounded interaction with web-page content.

Unlike ``tools.py``'s read-only trio (``browse_to``, ``get_current_page``,
``list_browser_tabs``), everything here can act on the page: click a
control, fill a field, submit a form. Every write here is addressed by a
*handle* from :func:`read_page_manifest`, never by a guessed CSS selector or
coordinate — see ``manifest_js.py`` for the grounding mechanism.

Each tool also takes a ``label`` argument: the control's own text, exactly
as the manifest reported it. Execution never uses ``label`` — only
``handle`` locates the element — but it does two other jobs. It is what a
confirmation prompt shows the user ("Click 'Add to Basket'?" rather than a
bare handle id), and it is what
:func:`jarvis.security.consequence.classify` inspects to decide whether a
click must always be confirmed individually — a payment button's own label
says "buy" or "checkout"; a search box's typed content does not.
"""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .observe import render_manifest, settle
from .tools import detect_browser, driver_for

_JS_PERMISSION_HINT = (
    ' isn\'t set up to let JARVIS read or act on page content. Open its Develop menu and turn on '
    '"Allow JavaScript from Apple Events" (Safari) or the equivalent automation setting, then try again.'
)


class ReadPageManifestTool(Tool):
    spec = ToolSpec(
        name="read_page_manifest",
        description=(
            "List the clickable, fillable and readable elements on the current web page, each "
            "with a stable handle for a later click_page_element/fill_page_field/submit_page_form call"
        ),
        parameters={
            "type": "object",
            "properties": {
                "browser": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0,
                           "description": "skip this many elements, to see more of a long page"},
                "roles": {"type": "array", "default": []},
            },
        },
        risk=RiskLevel.LOW,
        category="browser",
        requires_macos=True,
        requires_network=False,
        expected_ms=1800,
        returns="a list of {handle, role, text, name, href, value, rect} for each interactive element",
        examples=["what can I click on this page", "find the search box",
                 "list the products on this page"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("browser") or await detect_browser(self._deps)
        driver = driver_for(self._deps.controller, name)
        if not await driver.can_execute_js():
            return ToolResult.failure(f"{driver.app_name}{_JS_PERMISSION_HINT}")
        await settle(driver)
        manifest = await driver.page_manifest(limit=int(args.get("limit") or 50),
                                              roles=args.get("roles") or None,
                                              offset=int(args.get("offset") or 0))
        elements = manifest.get("elements")
        if not elements:
            return ToolResult.failure(
                f"{driver.app_name} didn't return any page elements.",
                detail=str(manifest.get("reason", "")), wrong_tool=False,
            )
        return ToolResult(
            data={"elements": elements, "url": manifest.get("url", ""),
                  "title": manifest.get("title", ""), "browser": driver.app_name,
                  "total": manifest.get("total"), "text": manifest.get("text", "")},
            summary=f"{len(elements)} elements found on {manifest.get('title') or 'the page'}.",
            observation=render_manifest(manifest),
            display={
                "kind": "list", "title": "Page elements",
                "items": [f"{e.get('role')}: {e.get('text') or e.get('name') or e.get('handle')}"
                         for e in elements[:30]],
            },
        )


class _HandleTool(Tool):
    """Shared by the tools that act on one page element by its handle."""

    def __init__(self, deps):
        self._deps = deps

    async def _driver(self, args: dict[str, Any]):
        name = args.get("browser") or await detect_browser(self._deps)
        return driver_for(self._deps.controller, name)

    async def _ready(self, args: dict[str, Any]):
        """The driver, once the page has stopped changing — a handle read
        from a page mid-re-render may point at an element about to go."""
        driver = await self._driver(args)
        await settle(driver)
        return driver

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        driver = await self._driver(args)
        target = await driver.inspect_handle(args["handle"])
        return target if target.get("found") else None

    @staticmethod
    def _name(args: dict[str, Any], result: dict[str, Any]) -> str:
        return result.get("text") or args.get("label") or args["handle"]

    @staticmethod
    def _stale(verb: str, name: str, result: dict[str, Any]) -> ToolResult:
        reason = result.get("reason") or "the page may have changed"
        return ToolResult.failure(
            f"Couldn’t {verb} “{name}” — {reason}. Look at the page again before trying another handle.",
            detail=str(result.get("reason", "")),
        )

    @staticmethod
    def _now_on(result: dict[str, Any]) -> str:
        return f"Now on: {result.get('title') or 'the page'} — {result.get('url', '')}"


class ClickPageElementTool(_HandleTool):
    spec = ToolSpec(
        name="click_page_element",
        description="Click an element on the current web page, addressed by its handle from the page listing",
        parameters={
            "type": "object",
            "properties": {
                "handle": {"type": "string",
                           "description": "the [handle] shown next to the element in the page listing"},
                "label": {"type": "string", "default": "",
                          "description": "the element's text as listed (for your own reference)"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["handle"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        requires_macos=True,
        mutates=True,
        retryable=False,
        expected_ms=1500,
        confirmation_template='Click "{label}" on the page?',
        examples=["click the search button", "click Add to Basket", "click that link"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args)
        result = await driver.click_handle(args["handle"])
        name = self._name(args, result)
        if not result.get("ok"):
            return self._stale("click", name, result)
        return ToolResult(
            data={"clicked": name, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f"Clicked “{name}”.",
            observation=f"Clicked “{name}”. {self._now_on(result)}",
        )


class FillPageFieldTool(_HandleTool):
    spec = ToolSpec(
        name="fill_page_field",
        description=("Type into a field on the current web page, or choose an option in a select, "
                     "addressed by its handle from the page listing"),
        parameters={
            "type": "object",
            "properties": {
                "handle": {"type": "string",
                           "description": "the [handle] shown next to the field in the page listing"},
                "label": {"type": "string", "default": "",
                          "description": "the field's name as listed (for your own reference)"},
                "text": {"type": "string",
                         "description": "what to type; for a select, the option to choose"},
                "submit": {"type": "boolean", "default": False,
                          "description": "press Enter / submit the enclosing form afterwards"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["handle", "text"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        requires_macos=True,
        mutates=True,
        retryable=False,
        expected_ms=1500,
        confirmation_template='Type into "{label}" on the page?',
        examples=["search for wireless mice", "type my postcode into the address field",
                  "choose Blue in the colour menu"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args)
        result = await driver.fill_handle(args["handle"], args["text"], submit=bool(args.get("submit")))
        name = self._name(args, result)
        if not result.get("ok"):
            return self._stale("fill in", name, result)
        if result.get("chose"):
            done = f"Chose “{result['chose']}” in “{name}”."
        else:
            done = ("Typed into and submitted" if result.get("submitted") else "Typed into") + f" “{name}”."
        return ToolResult(
            data={"filled": name, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=done,
            observation=f"{done} {self._now_on(result)}",
        )


class SubmitPageFormTool(_HandleTool):
    spec = ToolSpec(
        name="submit_page_form",
        description=(
            "Submit the form containing an element on the current web page, addressed by its "
            "handle from the page listing"
        ),
        parameters={
            "type": "object",
            "properties": {
                "handle": {"type": "string",
                           "description": "the [handle] of the form's submit control or any field in it"},
                "label": {"type": "string", "default": "",
                          "description": "the control's text as listed (for your own reference)"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["handle"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        requires_macos=True,
        mutates=True,
        retryable=False,
        expected_ms=2000,
        confirmation_template='Submit "{label}"?',
        examples=["submit the form", "search now"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args)
        result = await driver.submit_handle(args["handle"])
        name = self._name(args, result)
        if not result.get("ok"):
            return self._stale("submit", name, result)
        return ToolResult(
            data={"submitted": name, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f"Submitted “{name}”.",
            observation=f"Submitted “{name}”. {self._now_on(result)}",
        )


def page_tools(deps) -> list[Tool]:
    return [
        ReadPageManifestTool(deps),
        ClickPageElementTool(deps),
        FillPageFieldTool(deps),
        SubmitPageFormTool(deps),
    ]
