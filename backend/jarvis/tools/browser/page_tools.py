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
                "limit": {"type": "integer", "default": 60},
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
        manifest = await driver.page_manifest(limit=int(args.get("limit") or 60),
                                              roles=args.get("roles") or None)
        elements = manifest.get("elements")
        if not elements:
            return ToolResult.failure(
                f"{driver.app_name} didn't return any page elements.",
                detail=str(manifest.get("reason", "")), wrong_tool=False,
            )
        return ToolResult(
            data={"elements": elements, "url": manifest.get("url", ""),
                  "title": manifest.get("title", ""), "browser": driver.app_name},
            summary=f"{len(elements)} elements found on {manifest.get('title') or 'the page'}.",
            display={
                "kind": "list", "title": "Page elements",
                "items": [f"{e.get('role')}: {e.get('text') or e.get('name') or e.get('handle')}"
                         for e in elements[:30]],
            },
        )


class ClickPageElementTool(Tool):
    spec = ToolSpec(
        name="click_page_element",
        description="Click an element on the current web page, addressed by a handle from read_page_manifest",
        parameters={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "label": {"type": "string", "description":
                         "the element's own text, exactly as read_page_manifest reported it"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["handle", "label"],
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

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("browser") or await detect_browser(self._deps)
        driver = driver_for(self._deps.controller, name)
        label = args["label"]
        result = await driver.click_handle(args["handle"])
        if not result.get("ok"):
            reason = result.get("reason") or "the page may have changed"
            return ToolResult.failure(
                f'Couldn’t click "{label}" — {reason}. '
                "Read the page again before trying another handle.",
                detail=str(result.get("reason", "")),
            )
        return ToolResult(
            data={"clicked": label, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f'Clicked "{label}".',
        )


class FillPageFieldTool(Tool):
    spec = ToolSpec(
        name="fill_page_field",
        description="Type into a field on the current web page, addressed by a handle from read_page_manifest",
        parameters={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "label": {"type": "string", "description":
                         "the field's own name or placeholder, exactly as read_page_manifest reported it"},
                "text": {"type": "string"},
                "submit": {"type": "boolean", "default": False,
                          "description": "press Enter / submit the enclosing form afterwards"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["handle", "label", "text"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        requires_macos=True,
        mutates=True,
        retryable=False,
        expected_ms=1500,
        confirmation_template='Type into "{label}" on the page?',
        examples=["search for wireless mice", "type my postcode into the address field"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("browser") or await detect_browser(self._deps)
        driver = driver_for(self._deps.controller, name)
        label = args["label"]
        result = await driver.fill_handle(args["handle"], args["text"], submit=bool(args.get("submit")))
        if not result.get("ok"):
            reason = result.get("reason") or "the page may have changed"
            return ToolResult.failure(
                f'Couldn’t type into "{label}" — {reason}. '
                "Read the page again before trying another handle.",
                detail=str(result.get("reason", "")),
            )
        verb = "Typed and submitted" if result.get("submitted") else "Typed"
        return ToolResult(
            data={"filled": label, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f'{verb} into "{label}".',
        )


class SubmitPageFormTool(Tool):
    spec = ToolSpec(
        name="submit_page_form",
        description=(
            "Submit the form containing an element on the current web page, addressed by a "
            "handle from read_page_manifest"
        ),
        parameters={
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "label": {"type": "string", "description":
                         "the submit control's own text, exactly as read_page_manifest reported it"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["handle", "label"],
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

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("browser") or await detect_browser(self._deps)
        driver = driver_for(self._deps.controller, name)
        label = args["label"]
        result = await driver.submit_handle(args["handle"])
        if not result.get("ok"):
            return ToolResult.failure(
                f'Couldn’t submit "{label}" — {result.get("reason") or "the page may have changed"}.',
                detail=str(result.get("reason", "")),
            )
        return ToolResult(
            data={"submitted": label, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f'Submitted "{label}".',
        )


def page_tools(deps) -> list[Tool]:
    return [
        ReadPageManifestTool(deps),
        ClickPageElementTool(deps),
        FillPageFieldTool(deps),
        SubmitPageFormTool(deps),
    ]
