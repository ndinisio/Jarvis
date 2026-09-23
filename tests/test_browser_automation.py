"""Grounded web-page interaction: the JS payloads, the AppleScript↔JS bridge,
and the four page tools built on top of them."""

from __future__ import annotations

import json

from jarvis.tools.browser import manifest_js
from jarvis.tools.browser.page_tools import (
    ClickPageElementTool,
    FillPageFieldTool,
    ReadPageManifestTool,
    SubmitPageFormTool,
)
from jarvis.tools.browser.tools import (
    ChromiumDriver,
    SafariDriver,
    _as_applescript_literal,
    _parse_js_json,
    detect_browser,
)
from jarvis.tools.macos.controller import ShellResult

# -- manifest_js.py: pure script-building, no I/O ----------------------------

def test_manifest_script_bounds_the_limit_and_encodes_the_role_filter():
    script = manifest_js.build_manifest_script(limit=9999, roles=["button", "link"])
    assert "var limit = 200;" in script  # clamped to the documented ceiling
    assert '["button", "link"]' in script or '["button","link"]' in script.replace(" ", "")
    assert "var roleFilter = " in script

    unfiltered = manifest_js.build_manifest_script(limit=0)
    assert "var limit = 1;" in unfiltered  # clamped to the documented floor
    assert "var roleFilter = null;" in unfiltered


def test_handle_and_text_are_always_json_encoded_never_concatenated():
    """A stray quote in a scraped label or typed value must not be able to
    break out of the generated script — this is the concrete security
    property, not just a style preference."""
    hostile = 'a"; alert(1); var x="'
    script = manifest_js.build_fill_script(hostile, hostile, submit=True)
    # The hostile text appears only inside a JSON-encoded string literal...
    assert json.dumps(hostile) in script
    # ...and never as a raw, unescaped break-out of the surrounding script.
    assert 'jarvisFind(' + json.dumps(hostile) + ')' in script
    assert "var submitted = false;" in script
    assert "if (true) {" in script  # submit=True rendered as a JS literal, not a string


def test_click_and_submit_scripts_locate_by_the_given_handle():
    click = manifest_js.build_click_script("jv7")
    assert 'jarvisFind("jv7")' in click
    submit = manifest_js.build_submit_script("jv7")
    assert 'jarvisFind("jv7")' in submit


# -- the AppleScript↔JS embedding layer --------------------------------------

def test_applescript_literal_escapes_backslashes_quotes_and_newlines():
    raw = 'say "hi"\\nnext line'
    escaped = _as_applescript_literal(raw)
    assert '\\"' in escaped
    assert "\\\\" in escaped
    assert "\n" not in escaped  # never a raw embedded newline


def test_parse_js_json_fails_safely_on_empty_or_malformed_input():
    assert _parse_js_json("")["ok"] is False
    assert _parse_js_json("not json")["ok"] is False
    assert _parse_js_json("[1, 2, 3]")["ok"] is False  # valid JSON, wrong shape
    parsed = _parse_js_json('{"ok": true, "url": "https://x.example"}')
    assert parsed == {"ok": True, "url": "https://x.example"}


# -- drivers: run_js() builds the right AppleScript, base methods sit on it --

class _FakeController:
    def __init__(self, stdout: str = "", ok: bool = True):
        self.calls: list[tuple[str, float]] = []
        self._stdout = stdout
        self._ok = ok

    async def osascript(self, script: str, language: str = "AppleScript", timeout: float = 25.0):
        self.calls.append((script, timeout))
        returncode = 0 if self._ok else 1
        return ShellResult(returncode, self._stdout, "")

    async def frontmost_app(self) -> str:
        return "Google Chrome"


async def test_safari_driver_embeds_the_script_in_do_javascript():
    controller = _FakeController(stdout="2")
    driver = SafariDriver(controller)
    result = await driver.run_js("1+1")
    assert result == "2"
    script, _ = controller.calls[-1]
    assert 'do JavaScript "1+1" in front document' in script


async def test_chromium_driver_embeds_the_script_in_execute_javascript():
    controller = _FakeController(stdout="2")
    driver = ChromiumDriver(controller, "Google Chrome")
    result = await driver.run_js("1+1")
    assert result == "2"
    script, _ = controller.calls[-1]
    assert 'execute active tab of front window javascript "1+1"' in script


async def test_run_js_returns_empty_string_when_the_applescript_call_fails():
    controller = _FakeController(stdout="2", ok=False)
    driver = SafariDriver(controller)
    assert await driver.run_js("1+1") == ""


async def test_can_execute_js_reflects_whether_the_probe_actually_worked():
    working = SafariDriver(_FakeController(stdout="2"))
    assert await working.can_execute_js() is True

    broken = SafariDriver(_FakeController(stdout="", ok=False))
    assert await broken.can_execute_js() is False


async def test_page_manifest_click_fill_submit_are_built_on_run_js(monkeypatch):
    """The base-class grounded-interaction methods only need run_js — verify
    each one sends the manifest_js payload and parses the JSON it gets back,
    without caring which browser it is."""
    driver = SafariDriver(_FakeController())
    seen: list[str] = []

    async def fake_run_js(script, *, timeout=20.0):
        seen.append(script)
        if "jarvisFind" in script:
            return json.dumps({"ok": True, "url": "https://x.example", "title": "X"})
        return json.dumps({"elements": [{"handle": "jv1", "role": "button", "text": "Go"}],
                           "url": "https://x.example", "title": "X"})

    monkeypatch.setattr(driver, "run_js", fake_run_js)

    manifest = await driver.page_manifest(limit=10)
    assert manifest["elements"][0]["handle"] == "jv1"
    assert "var limit = 10;" in seen[-1]

    clicked = await driver.click_handle("jv1")
    assert clicked["ok"] is True

    filled = await driver.fill_handle("jv1", "hello", submit=True)
    assert filled["ok"] is True

    submitted = await driver.submit_handle("jv1")
    assert submitted["ok"] is True


async def test_detect_browser_prefers_the_frontmost_browser_app():
    class Deps:
        controller = _FakeController()

    assert await detect_browser(Deps()) == "Google Chrome"

    class NonBrowserFrontmost(_FakeController):
        async def frontmost_app(self):
            return "Finder"

    class Deps2:
        controller = NonBrowserFrontmost()

    assert await detect_browser(Deps2()) == "Safari"


# -- the four Tool subclasses, through their own run() -----------------------

def _install_fake_driver(monkeypatch, deps, driver):
    import jarvis.tools.browser.page_tools as page_tools_module

    monkeypatch.setattr(page_tools_module, "detect_browser", _async_return("Safari"))
    monkeypatch.setattr(page_tools_module, "driver_for", lambda controller, name: driver)


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


class _FakeDriver:
    app_name = "Safari"

    def __init__(self, *, js_ok=True, manifest=None, action_result=None):
        self._js_ok = js_ok
        self._manifest = manifest or {"elements": [], "url": "", "title": ""}
        self._action_result = action_result if action_result is not None else {"ok": True}

    async def can_execute_js(self):
        return self._js_ok

    async def run_js(self, script, *, timeout=20.0):
        return "complete" if self._js_ok else ""

    async def page_manifest(self, *, limit=60, roles=None, offset=0):
        return self._manifest

    async def inspect_handle(self, handle):
        return {"found": False}

    async def click_handle(self, handle):
        return self._action_result

    async def fill_handle(self, handle, text, *, submit=False):
        return self._action_result

    async def submit_handle(self, handle):
        return self._action_result


async def test_read_page_manifest_reports_the_permission_hint_when_js_is_off(app, ctx, monkeypatch):
    driver = _FakeDriver(js_ok=False)
    _install_fake_driver(monkeypatch, app.deps, driver)
    tool = ReadPageManifestTool(app.deps)
    outcome = await tool.run({"browser": "", "limit": 60, "roles": []}, ctx)
    assert outcome.ok is False
    assert "Allow JavaScript" in outcome.summary


async def test_read_page_manifest_returns_the_elements_it_found(app, ctx, monkeypatch):
    driver = _FakeDriver(manifest={
        "elements": [{"handle": "jv1", "role": "button", "text": "Add to Basket"}],
        "url": "https://x.example", "title": "X",
    })
    _install_fake_driver(monkeypatch, app.deps, driver)
    tool = ReadPageManifestTool(app.deps)
    outcome = await tool.run({"browser": "", "limit": 60, "roles": []}, ctx)
    assert outcome.ok is True
    assert outcome.data["elements"][0]["handle"] == "jv1"


def test_click_page_element_is_medium_risk_and_the_prompt_shows_the_label_not_a_bare_handle(app):
    """``click_page_element`` is ``requires_macos=True``, so exercising its
    confirmation through the live registry only works on a real Mac (see
    the ``requires_macos`` gate in ``ToolRegistry.call()``). What's tested
    here on any host is the piece that actually matters for this feature:
    the tool is MEDIUM risk (so it *will* be gated), and its
    ``confirmation_template`` renders the human-readable label the user
    would actually recognise — never the opaque DOM handle."""
    from jarvis.security.permissions import RiskLevel
    from jarvis.tools.registry import _confirmation_text

    spec = ClickPageElementTool(app.deps).spec
    assert spec.risk == RiskLevel.MEDIUM
    prompt = _confirmation_text(spec, {"handle": "jv9", "label": "Add to Basket"})
    assert prompt == 'Click "Add to Basket" on the page?'
    assert "jv9" not in prompt


async def test_click_page_element_reports_a_stale_handle_clearly(app, ctx, monkeypatch):
    driver = _FakeDriver(action_result={"ok": False, "reason": "stale handle"})
    _install_fake_driver(monkeypatch, app.deps, driver)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    tool = ClickPageElementTool(app.deps)
    outcome = await tool.run({"handle": "jv9", "label": "Add to Basket", "browser": ""}, ctx)
    assert outcome.ok is False
    assert "stale handle" in (outcome.error or "")


async def test_fill_page_field_reports_whether_it_also_submitted(app, ctx, monkeypatch):
    driver = _FakeDriver(action_result={"ok": True, "submitted": True,
                                        "url": "https://x.example", "title": "X"})
    _install_fake_driver(monkeypatch, app.deps, driver)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    tool = FillPageFieldTool(app.deps)
    outcome = await tool.run(
        {"handle": "jv2", "label": "Search", "text": "esp32", "submit": True, "browser": ""}, ctx
    )
    assert outcome.ok is True
    assert "submitted" in outcome.summary.lower()


async def test_submit_page_form_run_succeeds_and_reports_what_it_submitted(app, ctx, monkeypatch):
    driver = _FakeDriver(action_result={"ok": True, "submitted": True,
                                        "url": "https://x.example/results", "title": "Results"})
    _install_fake_driver(monkeypatch, app.deps, driver)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    outcome = await SubmitPageFormTool(app.deps).run(
        {"handle": "jv3", "label": "Search", "browser": ""}, ctx
    )
    assert outcome.ok is True
    assert outcome.data["submitted"] == "Search"
    assert "Search" in outcome.summary


async def test_submit_page_form_run_reports_a_stale_handle(app, ctx, monkeypatch):
    driver = _FakeDriver(action_result={"ok": False, "reason": "stale handle"})
    _install_fake_driver(monkeypatch, app.deps, driver)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    outcome = await SubmitPageFormTool(app.deps).run(
        {"handle": "jv3", "label": "Search", "browser": ""}, ctx
    )
    assert outcome.ok is False
    assert "stale handle" in (outcome.error or "")


async def test_submit_page_form_labelled_checkout_is_consequential_even_mid_task(app, ctx):
    """The safety net beyond "no checkout tool exists": a stray click on a
    checkout-labelled control must never be covered by a task grant."""
    from jarvis.security import consequence

    spec = SubmitPageFormTool(app.deps).spec
    assert consequence.classify("submit_page_form", {"label": "Proceed to Checkout"}, spec) is True
    assert consequence.classify("submit_page_form", {"label": "Search"}, spec) is False


# -- v3.0: what the model sees of a page -------------------------------------

def test_a_page_listing_shows_every_handle_the_model_needs():
    from jarvis.tools.browser.observe import render_manifest

    text = render_manifest({
        "url": "https://www.amazon.co.uk/dp/B0MOUSEM185", "title": "Mouse", "total": 90, "offset": 0,
        "elements": [
            {"handle": "jv41", "role": "select", "text": "Colour", "options": ["Select", "Blue"],
             "value": "Select"},
            {"handle": "jv42", "role": "button", "text": "Add to Basket"},
            {"handle": "jv7", "role": "checkbox", "text": "Gift wrap", "checked": False},
            {"handle": "jv3", "role": "link", "text": "Basket 0", "href": "/gp/cart/view.html",
             "visible": False},
        ],
        "dialogs": ["Added to Basket"], "text": "Logitech M185 £12.99",
    })
    assert "Page: Mouse — https://www.amazon.co.uk/dp/B0MOUSEM185" in text
    assert '[jv41] select "Colour" options=Select|Blue value="Select"' in text
    assert '[jv42] button "Add to Basket"' in text
    assert '[jv7] checkbox "Gift wrap" (unchecked)' in text
    assert '[jv3] link "Basket 0" → /gp/cart/view.html (below)' in text
    assert "offset=4" in text, "a long page says how to see more"
    assert "Open dialog: Added to Basket" in text
    assert "£12.99" in text


async def test_manifest_observation_carries_the_handles(app, ctx, monkeypatch):
    driver = _FakeDriver(manifest={
        "elements": [{"handle": "jv9", "role": "button", "text": "Add to Basket"}],
        "url": "https://x.example", "title": "X", "total": 1,
    })
    _install_fake_driver(monkeypatch, app.deps, driver)
    outcome = await ReadPageManifestTool(app.deps).run({"limit": 50, "roles": [], "offset": 0}, ctx)
    assert '[jv9] button "Add to Basket"' in outcome.for_model()
    assert outcome.summary == "1 elements found on X."


async def test_settling_waits_for_a_client_side_re_render():
    """The page must be still before JARVIS acts: a single-page app that
    re-renders a moment after a click would otherwise swallow what's typed
    next (the spa-add-two evaluation failure)."""
    from jarvis.tools.browser.observe import wait_until_quiet

    class Rerendering:
        def __init__(self):
            self.calls = 0

        async def run_js(self, script, *, timeout=20.0):
            self.calls += 1
            # Mutations keep arriving for the first few polls, then stop.
            return f"complete:u:{min(self.calls, 4)}:10"

    driver = Rerendering()
    assert await wait_until_quiet(driver, quiet_s=0.3, timeout_s=3.0) is True
    assert driver.calls >= 6, "it kept watching until the page had been still for a while"


async def test_settling_gives_up_fast_when_javascript_is_off():
    from jarvis.tools.browser.observe import wait_until_quiet, wait_until_ready

    class Silent:
        async def run_js(self, script, *, timeout=20.0):
            return ""

    import time as _time

    started = _time.monotonic()
    assert await wait_until_ready(Silent(), timeout_s=5.0) is False
    assert await wait_until_quiet(Silent(), timeout_s=5.0) is False
    assert _time.monotonic() - started < 2.0
