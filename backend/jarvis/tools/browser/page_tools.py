"""Grounded interaction with web-page content.

Unlike ``tools.py``'s read-only trio (``browse_to``, ``get_current_page``,
``list_browser_tabs``), everything here can act on the page: click a
control, fill a field, submit a form, press a key, scroll, go back. Every
write here is addressed by a *handle* from :func:`read_page_manifest`, never
by a guessed CSS selector or coordinate — see ``manifest_js.py`` for the
grounding mechanism.

Which browser a call reaches — the user's everyday browser or JARVIS Chrome
— is the :class:`~jarvis.surfaces.web.hub.BrowserHub`'s decision, made once
per task, so every step of an errand happens in the same window.

Each handle tool also takes a ``label`` argument: the control's text as the
model read it. Execution never uses it — only ``handle`` locates the
element — and neither does the permission gate, which judges the element
the handle really points at (:meth:`Tool.inspect`). The label is only for
the model's own bookkeeping and as a fallback name in messages.

Some things are the user's alone: typing a password or card number (the
fill primitives refuse), solving a CAPTCHA, approving a sign-in.
``ask_user_to_take_over`` pauses the task, shows the browser window and
waits for the user to say they're done.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ...core.errors import ConfirmationDeclined
from ...security.permissions import RiskLevel
from ...surfaces.web.hub import hub_of
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .observe import PageMemory, render_manifest, settle
from .tools import PAGE_KEYS, normalise_key

_JS_PERMISSION_HINT = (
    ' isn\'t set up to let JARVIS read or act on page content. Open its Develop menu and turn on '
    '"Allow JavaScript from Apple Events" (Safari) or the equivalent automation setting, then try again.'
)

_NO_BROWSER = ("No browser is reachable: on this computer JARVIS needs its own browser — "
               'install it with pip install -e ".[browser]".')

_BROWSER_PARAM = {"type": "string", "default": "",
                  "description": "only to insist on a particular browser; normally leave empty"}


async def _driver_for(deps, ctx: ToolContext | None, args: dict[str, Any], *, navigating: bool = False,
                      url: str = ""):
    return await hub_of(deps).for_action(ctx, navigating=navigating, url=url,
                                         browser=str(args.get("browser") or ""))


def _now_on(result: dict[str, Any]) -> str:
    return f"Now on: {result.get('title') or 'the page'} — {result.get('url', '')}"


class ReadPageManifestTool(Tool):
    spec = ToolSpec(
        name="read_page_manifest",
        description=("List what's on the current web page to click, fill or read, each with a "
                     "[handle] to act on"),
        parameters={
            "type": "object",
            "properties": {
                "browser": _BROWSER_PARAM,
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0,
                           "description": "skip this many elements, to see more of a long page"},
                "roles": {"type": "array", "default": []},
            },
        },
        risk=RiskLevel.LOW,
        category="browser",
        requires_network=False,
        expected_ms=1800,
        returns="the page's elements, each with a [handle], plus open dialogs and page text",
        examples=["what can I click on this page", "find the search box",
                 "list the products on this page"],
    )

    def __init__(self, deps):
        self._deps = deps
        self._memory = PageMemory()

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await _driver_for(self._deps, ctx, args)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
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
        changes = self._memory.changes((ctx.task_id, id(driver)), manifest)
        return ToolResult(
            data={"elements": elements, "url": manifest.get("url", ""),
                  "title": manifest.get("title", ""), "browser": driver.app_name,
                  "total": manifest.get("total"), "text": manifest.get("text", ""),
                  "signals": manifest.get("signals") or {}},
            summary=f"{len(elements)} elements found on {manifest.get('title') or 'the page'}.",
            observation=render_manifest(manifest, changes=changes),
            display={
                "kind": "list", "title": "Page elements",
                "items": [f"{e.get('role')}: {e.get('text') or e.get('name') or e.get('handle')}"
                         for e in elements[:30]],
            },
        )


class _PageTool(Tool):
    """Shared by the tools that act on the current page."""

    def __init__(self, deps):
        self._deps = deps

    async def _driver(self, args: dict[str, Any], ctx: ToolContext | None = None):
        return await _driver_for(self._deps, ctx, args)

    async def _ready(self, args: dict[str, Any], ctx: ToolContext | None = None):
        """The driver, once the page has stopped changing — a handle read
        from a page mid-re-render may point at an element about to go."""
        driver = await self._driver(args, ctx)
        if driver is not None:
            await settle(driver)
        return driver


class _HandleTool(_PageTool):
    """Shared by the tools that act on one page element by its handle."""

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        driver = await self._driver(args, ctx)
        if driver is None:
            return None
        target = await driver.inspect_handle(args["handle"])
        return target if target.get("found") else None

    @staticmethod
    def _name(args: dict[str, Any], result: dict[str, Any]) -> str:
        return result.get("text") or args.get("label") or args["handle"]

    @staticmethod
    def _stale(verb: str, name: str, result: dict[str, Any]) -> ToolResult:
        reason = result.get("reason") or "the page may have changed"
        if result.get("refused"):
            return ToolResult.failure(f"Didn’t {verb} “{name}” — {reason}.", detail=reason)
        return ToolResult.failure(
            f"Couldn’t {verb} “{name}” — {reason}. Look at the page again before trying another handle.",
            detail=str(result.get("reason", "")),
        )


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
                "browser": _BROWSER_PARAM,
            },
            "required": ["handle"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        mutates=True,
        retryable=False,
        expected_ms=1500,
        confirmation_template='Click "{label}" on the page?',
        examples=["click the search button", "click Add to Basket", "click that link"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        result = await driver.click_handle(args["handle"])
        name = self._name(args, result)
        if not result.get("ok"):
            return self._stale("click", name, result)
        return ToolResult(
            data={"clicked": name, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f"Clicked “{name}”.",
            observation=f"Clicked “{name}”. {_now_on(result)}",
        )


class FillPageFieldTool(_HandleTool):
    spec = ToolSpec(
        name="fill_page_field",
        description=("Type into a field on the current web page, or choose an option in a select, "
                     "addressed by its handle from the page listing — never for passwords or card details"),
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
                "browser": _BROWSER_PARAM,
            },
            "required": ["handle", "text"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        mutates=True,
        retryable=False,
        expected_ms=1500,
        confirmation_template='Type into "{label}" on the page?',
        examples=["search for wireless mice", "type my postcode into the address field",
                  "choose Blue in the colour menu"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
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
            observation=f"{done} {_now_on(result)}",
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
                "browser": _BROWSER_PARAM,
            },
            "required": ["handle"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        mutates=True,
        retryable=False,
        expected_ms=2000,
        confirmation_template='Submit "{label}"?',
        examples=["submit the form", "search now"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        result = await driver.submit_handle(args["handle"])
        name = self._name(args, result)
        if not result.get("ok"):
            return self._stale("submit", name, result)
        return ToolResult(
            data={"submitted": name, "url": result.get("url", ""), "title": result.get("title", "")},
            summary=f"Submitted “{name}”.",
            observation=f"Submitted “{name}”. {_now_on(result)}",
        )


class PressPageKeyTool(_PageTool):
    spec = ToolSpec(
        name="press_page_key",
        description=("Press a key on the current web page — Enter, Escape (close a pop-up), Tab, "
                     "the arrow keys (move through suggestions), Page Down…; sent to the element with "
                     "the given handle, or to whatever has focus"),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string",
                        "enum": sorted({k for k in PAGE_KEYS if not k.startswith("arrow") and k != "esc"})},
                "handle": {"type": "string", "default": "",
                           "description": "the element to press it on; empty for the focused one"},
                "browser": _BROWSER_PARAM,
            },
            "required": ["key"],
        },
        risk=RiskLevel.MEDIUM,
        category="browser",
        mutates=True,
        retryable=False,
        expected_ms=800,
        confirmation_template='Press {key} on the page?',
        examples=["press escape to close the pop-up", "press enter", "go down to the first suggestion"],
    )

    async def inspect(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        driver = await self._driver(args, ctx)
        if driver is None:
            return None
        handle = args.get("handle") or await driver.focused_handle()
        if not handle:
            return None
        target = await driver.inspect_handle(handle)
        return target if target.get("found") else None

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        key = normalise_key(args["key"])
        if not key:
            return ToolResult.failure(f"“{args['key']}” isn't a key JARVIS presses on web pages.")
        driver = await self._ready(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        result = await driver.press_key(key, args.get("handle") or "")
        if not result.get("ok"):
            return ToolResult.failure(f"Couldn’t press {args['key']} — {result.get('reason', 'no response')}.")
        await settle(driver)
        page = await driver.current_page()
        shown = "Space" if key == " " else key
        return ToolResult(data={"key": shown, **page}, summary=f"Pressed {shown}.",
                          observation=f"Pressed {shown}. {_now_on(page)}")


class ScrollPageTool(_PageTool):
    spec = ToolSpec(
        name="scroll_page",
        description=("Scroll the current web page down or up (to load more results or reach content "
                     "further down), to the top or bottom, or bring the element with a handle into view"),
        parameters={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"], "default": "down"},
                "handle": {"type": "string", "default": "",
                           "description": "scroll this element into view instead"},
                "browser": _BROWSER_PARAM,
            },
        },
        risk=RiskLevel.LOW,
        category="browser",
        expected_ms=700,
        examples=["scroll down", "load more results", "go to the bottom of the page"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._ready(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        result = await driver.scroll(args.get("direction") or "down", args.get("handle") or "")
        if not result.get("ok"):
            return ToolResult.failure(f"Couldn’t scroll — {result.get('reason', 'no response')}.")
        await settle(driver)
        y, height, view = (int(result.get(k) or 0) for k in ("y", "height", "view"))
        where = ""
        if height and view:
            if y + view >= height - 4:
                where = " (at the bottom of the page)"
            elif y <= 0:
                where = " (at the top of the page)"
        return ToolResult(data=result, summary=f"Scrolled{where}.", observation=f"Scrolled{where}.")


class PageGoBackTool(_PageTool):
    spec = ToolSpec(
        name="page_go_back",
        description="Go back to the previous page in the browser (like the Back button)",
        parameters={"type": "object", "properties": {"browser": _BROWSER_PARAM}},
        risk=RiskLevel.LOW,
        category="browser",
        expected_ms=1200,
        examples=["go back", "back to the results"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._driver(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        result = await driver.go_back()
        if not result.get("ok"):
            return ToolResult.failure(f"Couldn’t go back — {result.get('reason', 'no response')}.")
        await settle(driver)
        page = await driver.current_page()
        return ToolResult(data=page, summary="Went back.", observation=f"Went back. {_now_on(page)}")


class WaitForPageTool(_PageTool):
    spec = ToolSpec(
        name="wait_for_page",
        description=("Wait until the current web page shows some text, or its address contains "
                     "something — for results, confirmations or pages that take a while to appear"),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "default": "", "description": "text that should appear"},
                "url_contains": {"type": "string", "default": ""},
                "timeout_s": {"type": "number", "default": 10},
                "browser": _BROWSER_PARAM,
            },
        },
        risk=RiskLevel.LOW,
        category="browser",
        expected_ms=3000,
        examples=["wait for the results to load", "wait until it says order received"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = str(args.get("text") or "").strip()
        fragment = str(args.get("url_contains") or "").strip()
        if not text and not fragment:
            return ToolResult.failure("Say what to wait for: some text, or part of the address.")
        driver = await self._driver(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        timeout = max(1.0, min(float(args.get("timeout_s") or 10), 30.0))
        deadline = time.monotonic() + timeout
        page: dict[str, str] = {}
        while True:
            ctx.raise_if_cancelled()
            page = await driver.current_page()
            url_ok = not fragment or fragment.lower() in str(page.get("url", "")).lower()
            if url_ok and (not text or await driver.has_text(text)):
                wanted = f"“{text}”" if text else f"an address with “{fragment}”"
                return ToolResult(data={"found": True, **page}, summary=f"The page shows {wanted}.",
                                  observation=f"The page now shows {wanted}. {_now_on(page)}")
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.4)
        wanted = f"“{text}”" if text else f"an address containing “{fragment}”"
        return ToolResult.failure(f"After {timeout:.0f}s the page still doesn't show {wanted}.",
                                  detail=_now_on(page), wrong_tool=False)


class TakeOverTool(_PageTool):
    spec = ToolSpec(
        name="ask_user_to_take_over",
        description=("Pause and ask the user to do a step only they should do in the browser — sign in, "
                     "pass a CAPTCHA or two-factor check, enter payment details — then carry on once "
                     "they say they're done"),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "what they need to do, e.g. 'sign in to Amazon'"},
                "browser": _BROWSER_PARAM,
            },
            "required": ["reason"],
        },
        # Asking is not acting; the wait itself is never pre-approved (see
        # PermissionBroker.require's *handoff*).
        risk=RiskLevel.LOW,
        category="browser",
        expected_ms=60000,
        examples=["I need you to sign in first", "please solve the CAPTCHA"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        driver = await self._driver(args, ctx)
        if driver is None:
            return ToolResult.failure(_NO_BROWSER)
        reason = str(args.get("reason") or "finish this step").strip().rstrip(".")
        await driver.bring_to_front()
        page = await driver.current_page()
        request = f"Please {_lower_first(reason)} in the {driver.app_name} window, then say “done”."
        ctx.report(f"Over to you — {request[0].lower()}{request[1:]}")
        try:
            await ctx.permissions.require(
                self.spec.name, "low", request,
                {"url": page.get("url", ""), "browser": driver.app_name, "reason": reason},
                handoff=True, timeout_s=float(ctx.config.browser.handoff_timeout_s),
                task_id=ctx.task_id,
            )
        except ConfirmationDeclined:
            return ToolResult.failure(
                f"The user didn't take over ({reason}), so the task can't go past this point.",
                wrong_tool=False,
            )
        await settle(driver)
        page = await driver.current_page()
        return ToolResult(data={"handed_back": True, **page}, summary="You're done — carrying on.",
                          observation=("The user says they've finished. Look at the page again before "
                                       f"continuing. {_now_on(page)}"))


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def page_tools(deps) -> list[Tool]:
    return [
        ReadPageManifestTool(deps),
        ClickPageElementTool(deps),
        FillPageFieldTool(deps),
        SubmitPageFormTool(deps),
        PressPageKeyTool(deps),
        ScrollPageTool(deps),
        PageGoBackTool(deps),
        WaitForPageTool(deps),
        TakeOverTool(deps),
    ]
