"""HomeKit, via a named Shortcut.

Home.app has no AppleScript dictionary, so there's nothing to build a
grounded fixture against the way Calendar/Reminders/Contacts tests do —
these mock the one real bridge, controller.run(["shortcuts", ...]),
exactly like test_downloads.py already does for the installer tool's own
argv-based system-binary calls.

capabilities.homekit defaults to False (unlike every other capability
flag — see core/config.py), so every test here enables it first. A test
that forgets that step doesn't error loudly: the registry just reports
"I don't have a tool called ..." with ok=False, which can accidentally
satisfy a bare `assert not result.ok` for entirely the wrong reason. The
enabling step is centralised in one fixture specifically to make that
mistake structurally hard to repeat.
"""

from __future__ import annotations

import asyncio

import pytest
from jarvis.tools.homekit.tools import RunHomeShortcutTool, homekit_tools
from jarvis.tools.macos.controller import ShellResult


@pytest.fixture
def enabled(app, monkeypatch):
    app.config_store.update({"capabilities": {"homekit": True}})
    for name in ("list_home_shortcuts", "run_home_shortcut"):
        monkeypatch.setattr(app.deps.registry.get(name).spec, "requires_macos", False)
    return app


def _fake_run(monkeypatch, app, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    calls: list[list[str]] = []

    async def fake_run(argv, timeout=20.0, stdin=None):
        calls.append(argv)
        return ShellResult(returncode, stdout, stderr)

    monkeypatch.setattr(app.deps.controller, "run", fake_run)
    return calls


# -- capability flag -----------------------------------------------------------

async def test_homekit_tools_are_absent_until_the_capability_flag_is_enabled(app, ctx):
    assert app.deps.registry.get("list_home_shortcuts") is None
    assert app.deps.registry.get("run_home_shortcut") is None
    assert "homekit" not in app.capabilities


async def test_homekit_tools_and_capability_appear_once_enabled(enabled):
    assert enabled.deps.registry.get("list_home_shortcuts") is not None
    assert enabled.deps.registry.get("run_home_shortcut") is not None
    assert "homekit" in enabled.capabilities


# -- ListHomeShortcutsTool ----------------------------------------------------

async def test_list_home_shortcuts_parses_one_name_per_line(enabled, ctx, monkeypatch):
    calls = _fake_run(app=enabled, monkeypatch=monkeypatch,
                      stdout="Turn Off Lights\nMovie Night\nGood Morning\n")
    result = await enabled.deps.registry.call("list_home_shortcuts", {}, ctx)
    assert result.ok
    assert result.data["shortcuts"] == ["Turn Off Lights", "Movie Night", "Good Morning"]
    assert calls == [["/usr/bin/shortcuts", "list"]]


async def test_list_home_shortcuts_reports_none_set_up(enabled, ctx, monkeypatch):
    _fake_run(app=enabled, monkeypatch=monkeypatch, stdout="")
    result = await enabled.deps.registry.call("list_home_shortcuts", {}, ctx)
    assert result.ok
    assert result.data["shortcuts"] == []
    assert "no shortcuts" in result.summary.lower()


async def test_list_home_shortcuts_reports_a_failure_from_the_binary_itself(enabled, ctx, monkeypatch):
    _fake_run(app=enabled, monkeypatch=monkeypatch, returncode=1,
             stderr="shortcuts: command failed")
    result = await enabled.deps.registry.call("list_home_shortcuts", {}, ctx)
    assert not result.ok
    assert "couldn't list" in result.summary.lower()


async def test_list_home_shortcuts_ignores_blank_lines(enabled, ctx, monkeypatch):
    _fake_run(app=enabled, monkeypatch=monkeypatch, stdout="Turn Off Lights\n\n\nMovie Night\n")
    result = await enabled.deps.registry.call("list_home_shortcuts", {}, ctx)
    assert result.data["shortcuts"] == ["Turn Off Lights", "Movie Night"]


# -- RunHomeShortcutTool -------------------------------------------------------

async def test_run_home_shortcut_uses_argv_never_a_shell_string(enabled, ctx, monkeypatch):
    calls = _fake_run(app=enabled, monkeypatch=monkeypatch, stdout="")
    enabled.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                              "always_confirm": []}})
    result = await enabled.deps.registry.call("run_home_shortcut", {"name": "Turn Off Lights"}, ctx)
    assert result.ok
    assert calls == [["/usr/bin/shortcuts", "run", "Turn Off Lights"]]


async def test_run_home_shortcut_requires_a_name(enabled, ctx):
    outcome = await RunHomeShortcutTool(enabled.deps).run({}, ctx)
    assert outcome.ok is False


async def test_run_home_shortcut_reports_a_failure_from_the_binary_itself(enabled, ctx, monkeypatch):
    _fake_run(app=enabled, monkeypatch=monkeypatch, returncode=1, stderr="No shortcut named X")
    enabled.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                              "always_confirm": []}})
    result = await enabled.deps.registry.call("run_home_shortcut", {"name": "Nonexistent"}, ctx)
    assert not result.ok
    assert "didn't run" in result.summary.lower()


async def test_a_quote_in_the_name_never_escapes_the_argv_element(enabled, ctx, monkeypatch):
    """argv-based execution (asyncio.create_subprocess_exec, no shell) means
    there is no escaping to get wrong — a quote, backtick or `; rm -rf` in
    the name is just literal text within one argv element, never
    interpreted."""
    calls = _fake_run(app=enabled, monkeypatch=monkeypatch, returncode=127,
                      stderr="command not found")
    dangerous = 'Lights"; rm -rf ~; echo "pwned'
    enabled.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                              "always_confirm": []}})
    await enabled.deps.registry.call("run_home_shortcut", {"name": dangerous}, ctx)
    assert calls == [["/usr/bin/shortcuts", "run", dangerous]]  # one untouched argv element


async def test_run_home_shortcut_is_high_risk_and_always_confirms_individually(enabled):
    spec = enabled.deps.registry.get("run_home_shortcut").spec
    assert spec.risk == "high"
    assert spec.always_confirm_individually is True


async def test_confirmation_template_names_the_exact_shortcut_and_the_visibility_caveat(
    enabled, monkeypatch
):
    calls = _fake_run(app=enabled, monkeypatch=monkeypatch, stdout="")

    async def approve_soon():
        for _ in range(50):
            pending = enabled.permissions.pending()
            if pending:
                assert pending[0]["summary"] == (
                    'Run the "Turn Off Lights" Shortcut? I can\'t see what it actually does.'
                )
                enabled.permissions.resolve(pending[0]["id"], True)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no confirmation was ever requested")

    asyncio.create_task(approve_soon())
    result = await enabled.deps.registry.call("run_home_shortcut", {"name": "Turn Off Lights"},
                                              enabled.deps.tool_context())
    assert result.ok
    assert calls  # the shortcut actually ran once approved


async def test_run_home_shortcut_is_never_covered_by_a_remembered_session_grant(enabled, monkeypatch):
    """Mirrors the same guarantee proven for send_message in v2.2 — HIGH
    risk plus always_confirm_individually means a "yes, remember" on one
    call must never silently cover the next one, for a tool that runs
    something JARVIS cannot see the contents of most of all."""
    _fake_run(app=enabled, monkeypatch=monkeypatch, stdout="")
    enabled.config_store.update({"security": {"confirmation_timeout_s": 0.15}})

    async def approve_with_memory():
        for _ in range(50):
            pending = enabled.permissions.pending()
            if pending:
                enabled.permissions.resolve(pending[0]["id"], True, remember=True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_with_memory())
    first = await enabled.deps.registry.call("run_home_shortcut", {"name": "Turn Off Lights"},
                                             enabled.deps.tool_context())
    assert first.ok

    second = await enabled.deps.registry.call("run_home_shortcut", {"name": "Turn Off Lights"},
                                               enabled.deps.tool_context())
    assert not second.ok
    assert "confirmation_declined" in (second.error or "")


def test_homekit_tools_factory_returns_both_tools(app):
    names = {tool.spec.name for tool in homekit_tools(app.deps)}
    assert names == {"list_home_shortcuts", "run_home_shortcut"}


# -- HomeKitCapability.plan(): grounding against what's actually available ------

async def test_plan_falls_back_to_listing_when_nothing_is_set_up(enabled, fake_provider, monkeypatch):
    from jarvis.capabilities.base import Request

    _fake_run(app=enabled, monkeypatch=monkeypatch, stdout="")  # no shortcuts at all
    capability = enabled.capabilities["homekit"]
    plan = await capability.plan(Request(text="turn off the lights",
                                         ctx=enabled.deps.tool_context()))
    assert plan == {"tool": "list_home_shortcuts", "args": {}}
    assert fake_provider.calls == []  # nothing to match against, so no model call either


async def test_plan_picks_the_exact_match_the_model_names(enabled, fake_provider, monkeypatch):
    from jarvis.capabilities.base import Request

    _fake_run(app=enabled, monkeypatch=monkeypatch,
             stdout="Turn Off Lights\nMovie Night\n")
    fake_provider.json_responses.append('{"name": "Turn Off Lights"}')

    capability = enabled.capabilities["homekit"]
    plan = await capability.plan(Request(text="turn off the lights",
                                         ctx=enabled.deps.tool_context()))
    assert plan == {"tool": "run_home_shortcut", "args": {"name": "Turn Off Lights"}}


async def test_plan_never_runs_a_hallucinated_name_not_in_the_real_list(
    enabled, fake_provider, monkeypatch
):
    """The critical safety property of this whole capability: JARVIS cannot
    see what a Shortcut does, so the one thing standing between "turn off
    the lights" and running something unrelated is never proposing a name
    that doesn't actually exist. A model that ignores its own instructions
    and invents a plausible-sounding name must still be caught here, in
    code, not trusted."""
    from jarvis.capabilities.base import Request

    _fake_run(app=enabled, monkeypatch=monkeypatch,
             stdout="Turn Off Lights\nMovie Night\n")
    fake_provider.json_responses.append('{"name": "Delete All Photos"}')  # not in the list

    capability = enabled.capabilities["homekit"]
    plan = await capability.plan(Request(text="turn off the lights",
                                         ctx=enabled.deps.tool_context()))
    assert plan == {"tool": "list_home_shortcuts", "args": {}}


async def test_plan_falls_back_to_listing_when_the_model_finds_no_match(
    enabled, fake_provider, monkeypatch
):
    from jarvis.capabilities.base import Request

    _fake_run(app=enabled, monkeypatch=monkeypatch,
             stdout="Turn Off Lights\nMovie Night\n")
    fake_provider.json_responses.append('{"name": ""}')

    capability = enabled.capabilities["homekit"]
    plan = await capability.plan(Request(text="play some jazz",
                                         ctx=enabled.deps.tool_context()))
    assert plan == {"tool": "list_home_shortcuts", "args": {}}


async def test_plan_falls_back_to_listing_when_the_model_is_unavailable(
    enabled, fake_provider, monkeypatch
):
    from jarvis.capabilities.base import Request

    _fake_run(app=enabled, monkeypatch=monkeypatch,
             stdout="Turn Off Lights\nMovie Night\n")
    fake_provider.fail = True

    capability = enabled.capabilities["homekit"]
    plan = await capability.plan(Request(text="turn off the lights",
                                         ctx=enabled.deps.tool_context()))
    assert plan == {"tool": "list_home_shortcuts", "args": {}}


async def test_homekit_capability_runs_end_to_end_through_handle(enabled, fake_provider, monkeypatch):
    from jarvis.capabilities.base import Request

    calls = _fake_run(app=enabled, monkeypatch=monkeypatch,
                      stdout="Turn Off Lights\nMovie Night\n")
    fake_provider.json_responses.append('{"name": "Turn Off Lights"}')
    enabled.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                              "always_confirm": []}})
    for name in ("list_home_shortcuts", "run_home_shortcut"):
        monkeypatch.setattr(enabled.deps.registry.get(name).spec, "requires_macos", False)

    capability = enabled.capabilities["homekit"]
    response = await capability.handle(
        Request(text="turn off the lights", ctx=enabled.deps.tool_context())
    )
    assert "Turn Off Lights" in response.text
    assert calls[-1] == ["/usr/bin/shortcuts", "run", "Turn Off Lights"]
