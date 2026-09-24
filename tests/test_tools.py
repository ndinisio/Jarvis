"""Tool contracts: schemas, validation, permissions and error handling."""

from __future__ import annotations

import pytest
from jarvis.security.permissions import RiskLevel
from jarvis.tools.base import ToolResult, ToolSpec
from jarvis.tools.macos.tools import normalise_url


def test_every_tool_declares_a_complete_spec(app):
    for tool in app.deps.registry._tools.values():
        spec = tool.spec
        assert spec.name and spec.name.islower()
        assert spec.description and not spec.description.endswith(".")
        assert spec.risk in {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH}
        assert spec.parameters.get("type") == "object"
        assert isinstance(spec.parameters.get("properties", {}), dict)
        assert spec.category
        for name, definition in spec.parameters["properties"].items():
            assert "type" in definition, f"{spec.name}.{name} has no type"


def test_tool_names_are_unique_and_registered(app):
    names = app.deps.registry.names()
    assert len(names) == len(set(names))
    for expected in ["open_application", "read_clipboard", "write_clipboard", "capture_screen",
                     "analyse_screen", "get_system_info", "get_battery", "get_storage",
                     "search_web", "open_url", "read_email", "search_email",
                     "create_calendar_event", "read_calendar", "read_file", "write_file",
                     "run_shell_command"]:
        assert expected in names


def test_automation_flag_disables_every_new_tool_registration(app):
    """caps.automation=False must disable page_tools, download_tools and
    installer_tools together — not just the AutomationCapability that
    consumes them (that's covered separately in test_intelligence.py's
    test_automation_is_disabled_by_the_capability_flag)."""
    automation_only_tools = {
        "read_page_manifest", "click_page_element", "fill_page_field", "submit_page_form",
        "download_file", "run_installer",
    }
    before = set(app.deps.registry.names())
    assert automation_only_tools <= before, "sanity check: these must be registered by default"

    app.config_store.update({"capabilities": {"automation": False}})
    after = set(app.deps.registry.names())
    assert not (automation_only_tools & after), (
        f"still registered with automation off: {automation_only_tools & after}")
    # Unrelated tools must be unaffected.
    assert "browse_to" in after and "write_file" in after


def test_automation_flag_also_removes_the_capability_itself(app):
    """The same flag that hides the tools must also hide the capability
    that would otherwise be left dangling with none of its tools
    available — build_capabilities(deps) must not register it."""
    assert "automation" in app.capabilities
    app.config_store.update({"capabilities": {"automation": False}})
    assert "automation" not in app.capabilities


def test_page_tools_also_require_the_browser_flag(app):
    """read_page_manifest etc. build on the same driver browse_to/
    get_current_page use — they must not outlive caps.browser being off,
    even with caps.automation still on."""
    app.config_store.update({"capabilities": {"browser": False}})
    names = set(app.deps.registry.names())
    assert "browse_to" not in names
    assert "read_page_manifest" not in names
    assert "click_page_element" not in names


def test_download_and_installer_tools_also_require_the_files_flag(app):
    """download_file/run_installer are registered under the files-tools
    block — caps.files=False must remove them too, even with
    caps.automation still on, the same nesting page_tools has with
    caps.browser."""
    app.config_store.update({"capabilities": {"files": False}})
    names = set(app.deps.registry.names())
    assert "write_file" not in names
    assert "download_file" not in names
    assert "run_installer" not in names
    # Automation-but-not-files-dependent tools must be unaffected.
    assert "click_page_element" in names


def test_native_interaction_additions_follow_the_screen_flag(app):
    """wait_for_element/scroll/list_windows are registered alongside the
    existing click_element/type_text under caps.screen, same as before —
    not a new, separately-gated flag."""
    for name in ("wait_for_element", "scroll", "list_windows"):
        assert name in app.deps.registry.names()
    app.config_store.update({"capabilities": {"screen": False}})
    names = set(app.deps.registry.names())
    for name in ("wait_for_element", "scroll", "list_windows", "click_element"):
        assert name not in names


def test_high_risk_tools_are_marked(app):
    send_email = app.deps.registry.get("send_email")
    delete_file = app.deps.registry.get("delete_file")
    assert send_email.spec.risk == RiskLevel.HIGH
    assert delete_file.spec.risk == RiskLevel.HIGH


def test_validation_fills_defaults_and_rejects_missing(app):
    tool = app.deps.registry.get("get_time")
    assert tool.validate({})["field"] == "both"
    open_app = app.deps.registry.get("open_application")
    with pytest.raises(ValueError):
        open_app.validate({})


def test_validation_coerces_types(app):
    tool = app.deps.registry.get("set_volume")
    cleaned = tool.validate({"level": "42", "action": "set"})
    assert cleaned["level"] == 42
    with pytest.raises(ValueError):
        tool.validate({"action": "explode"})


async def test_unknown_tool_fails_gracefully(app, ctx):
    result = await app.deps.registry.call("does_not_exist", {}, ctx)
    assert result.ok is False
    assert "don't have a tool" in result.summary


async def test_tool_crash_is_contained(app, ctx):
    from jarvis.tools.base import Tool

    class Exploding(Tool):
        spec = ToolSpec(name="explode", description="Always fails", category="test")

        async def run(self, args, ctx):
            raise RuntimeError("boom")

    app.deps.registry.register(Exploding())
    result = await app.deps.registry.call("explode", {}, ctx)
    assert result.ok is False
    assert "boom" in (result.error or "")
    assert "boom" not in result.summary  # raw detail never reaches the user


async def test_tool_events_are_published(app, ctx):
    await app.deps.registry.call("get_time", {}, ctx)
    types = [e.type for e in app.bus.history]
    assert "tool.call" in types and "tool.result" in types


def test_url_normalisation():
    assert normalise_url("apple.com") == "https://apple.com"
    assert normalise_url("https://apple.com") == "https://apple.com"
    assert normalise_url("tide times").startswith("https://duckduckgo.com/?q=")


def test_describe_for_model_is_compact(app):
    listing = app.deps.registry.describe_for_model(["get_time", "get_battery"])
    assert listing.count("\n") == 1
    assert "get_time(" in listing


async def test_tool_result_shape():
    result = ToolResult.failure("Nope.", detail="why")
    assert result.ok is False and result.error == "why"
    assert result.as_dict()["summary"] == "Nope."


# --- v3.0: everyday commands -------------------------------------------------------

async def test_tab_shortcuts_go_to_the_browser_in_front(app, monkeypatch):
    from jarvis.tools.macos.controller import ShellResult
    from jarvis.tools.macos.everyday import BrowserTabTool

    scripts: list[str] = []

    async def osascript(script, *args, **kwargs):
        scripts.append(script)
        return ShellResult(0, "", "")

    async def frontmost():
        return "Google Chrome"

    monkeypatch.setattr(app.controller, "osascript", osascript)
    monkeypatch.setattr(app.controller, "frontmost_app", frontmost)
    result = await BrowserTabTool(app.deps).run({"action": "close", "browser": ""}, app.deps.tool_context())
    assert result.ok and "Closed the tab in Google Chrome" in result.summary
    assert 'tell application "Google Chrome" to activate' in scripts[0]
    assert 'keystroke "w" using command down' in scripts[0]


async def test_media_control_prefers_spotify_when_it_is_running(app, monkeypatch):
    from jarvis.tools.macos.controller import ShellResult
    from jarvis.tools.macos.everyday import MediaControlTool

    scripts: list[str] = []

    async def osascript(script, *args, **kwargs):
        scripts.append(script)
        return ShellResult(0, "", "")

    async def running(name):
        return name == "Spotify"

    monkeypatch.setattr(app.controller, "osascript", osascript)
    monkeypatch.setattr(app.controller, "is_app_running", running)
    result = await MediaControlTool(app.deps).run({"action": "next", "app": ""}, app.deps.tool_context())
    assert result.ok
    assert scripts == ['tell application "Spotify" to next track']
