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
