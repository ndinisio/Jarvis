"""The permission boundary: nothing risky runs unasked."""

from __future__ import annotations

import asyncio

import pytest
from jarvis.core.errors import ConfirmationDeclined, PermissionDenied
from jarvis.security.permissions import RiskLevel


def test_policy_follows_configuration(app):
    broker = app.permissions
    assert broker.policy_for(RiskLevel.LOW) == "allow"
    assert broker.policy_for(RiskLevel.MEDIUM) == "confirm"
    assert broker.policy_for(RiskLevel.HIGH) == "confirm"

    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    assert broker.policy_for(RiskLevel.MEDIUM) == "allow"
    assert broker.policy_for(RiskLevel.HIGH) == "confirm"


async def test_low_risk_needs_no_confirmation(app):
    assert await app.permissions.require("read_time", RiskLevel.LOW, "read the time") is True


async def test_high_risk_waits_for_approval(app):
    async def approve_soon():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                app.permissions.resolve(pending[0]["id"], True)
                return True
            await asyncio.sleep(0.01)
        return False

    approver = asyncio.create_task(approve_soon())
    granted = await app.permissions.require("send_email", RiskLevel.HIGH, "send a message")
    assert granted is True
    assert await approver is True


async def test_declined_confirmation_raises(app):
    async def decline_soon():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                app.permissions.resolve(pending[0]["id"], False)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(decline_soon())
    with pytest.raises(ConfirmationDeclined):
        await app.permissions.require("delete_file", RiskLevel.HIGH, "delete something")


async def test_confirmation_times_out_safely(app):
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    with pytest.raises(ConfirmationDeclined):
        await app.permissions.require("send_email", RiskLevel.HIGH, "send a message")
    assert app.permissions.pending() == []


async def test_session_grant_is_remembered(app):
    async def approve_with_memory():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                app.permissions.resolve(pending[0]["id"], True, remember=True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_with_memory())
    await app.permissions.require("move_file", RiskLevel.MEDIUM, "move a file")
    # The second call must not ask again.
    assert await asyncio.wait_for(
        app.permissions.require("move_file", RiskLevel.MEDIUM, "move a file"), timeout=0.5
    )


async def test_confirmation_request_is_published(app):
    asyncio.create_task(app.permissions.require("send_email", RiskLevel.HIGH, "send"))
    await asyncio.sleep(0.05)
    events = [e for e in app.bus.history if e.type == "confirm.request"]
    assert events and events[-1].payload["risk"] == "high"
    app.permissions.cancel_all()


def test_shell_allowlist_and_denylist(app):
    broker = app.permissions
    assert broker.check_shell("df -k /")[0] is True
    assert broker.check_shell("rm -rf /")[0] is False
    assert broker.check_shell("curl http://example.com")[0] is False
    assert broker.check_shell("ls | rm")[0] is False          # control characters
    assert broker.check_shell("sw_vers -productVersion")[0] is True

    app.config_store.update({"security": {"allow_shell": False}})
    with pytest.raises(PermissionDenied):
        broker.check_shell("ls")


async def test_registry_gates_medium_risk_tools(app, ctx):
    """A MEDIUM-risk tool must not execute while confirmation is outstanding."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.2}})
    result = await app.deps.registry.call("close_application", {"name": "Finder"}, ctx)
    assert result.ok is False
    assert "confirm" in (result.summary + str(result.error)).lower()


async def test_version_check_binaries_bypass_confirmation_only_for_the_version_flag(app):
    broker = app.permissions
    assert broker.check_shell("python3 --version")[0] is True
    assert broker.check_shell("python3 -V")[0] is True
    assert broker.check_shell("git --version")[0] is True
    # Any other invocation of the same binary still needs the normal escalation.
    assert broker.check_shell("python3 -m pip install anything")[0] is False
    assert broker.check_shell("python3 script.py")[0] is False


def test_lowercase_dash_v_is_never_auto_approved(app):
    """For python/python3, "-v" is verbose import tracing, not a version
    query, and with no script argument it reads from stdin and hangs rather
    than exiting — a silent, unbounded stall is the opposite of "a pure
    read with no side effect", so this must fall back to the normal
    HIGH-risk confirmation like any other non-allowlisted invocation."""
    assert app.permissions.check_shell("python3 -v")[0] is False


# -- task-scoped grants ------------------------------------------------------
# A long automation task pre-approves its own *routine* steps so a 30-step
# errand doesn't mean 30 prompts — but the grant is scoped to that one task
# id, and a call marked ``consequential`` must never be covered by it, no
# matter what was approved to start the task.

async def test_task_grant_covers_a_routine_call_with_no_prompt(app):
    app.permissions.grant_task("task-1")
    granted = await asyncio.wait_for(
        app.permissions.require("click_page_element", RiskLevel.MEDIUM, "click a button",
                                consequential=False, task_id="task-1"),
        timeout=0.5,
    )
    assert granted is True
    assert app.permissions.pending() == []  # never even asked


async def test_task_grant_never_covers_a_consequential_call(app):
    app.permissions.grant_task("task-1")

    async def approve_soon():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                app.permissions.resolve(pending[0]["id"], True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_soon())
    # Still has to ask, despite the task grant, because this call is marked
    # consequential — the approver above is what lets it complete at all.
    granted = await asyncio.wait_for(
        app.permissions.require("run_installer", RiskLevel.HIGH, "run an installer",
                                consequential=True, task_id="task-1"),
        timeout=1.0,
    )
    assert granted is True


async def test_task_grant_does_not_leak_to_a_different_task(app):
    app.permissions.grant_task("task-1")
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    with pytest.raises(ConfirmationDeclined):
        await app.permissions.require("click_page_element", RiskLevel.MEDIUM, "click a button",
                                      consequential=False, task_id="task-2")


async def test_revoke_task_clears_the_grant(app):
    app.permissions.grant_task("task-1")
    app.permissions.revoke_task("task-1")
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    with pytest.raises(ConfirmationDeclined):
        await app.permissions.require("click_page_element", RiskLevel.MEDIUM, "click a button",
                                      consequential=False, task_id="task-1")


async def test_a_suspended_confirmation_resumes_and_the_grant_then_covers_the_next_call(app):
    """Proves the pause-and-resume mechanics genuinely continue a multi-step
    sequence: the first (consequential) call really does suspend until
    answered, and only *after* that resolution does the task grant — set up
    before either call — cover the next, routine call with no further ask."""
    app.permissions.grant_task("task-1")

    async def approve_soon():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                app.permissions.resolve(pending[0]["id"], True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_soon())
    first = await asyncio.wait_for(
        app.permissions.require("send_email", RiskLevel.HIGH, "send a message",
                                consequential=True, task_id="task-1"),
        timeout=1.0,
    )
    assert first is True

    second = await asyncio.wait_for(
        app.permissions.require("click_page_element", RiskLevel.MEDIUM, "click a button",
                                consequential=False, task_id="task-1"),
        timeout=0.5,
    )
    assert second is True
    assert app.permissions.pending() == []


async def test_allow_session_grant_false_also_disables_a_task_grant(app):
    """allow_session_grant's name predates task grants, but its contract —
    "give me a fresh confirmation this time" — has to cover both
    pre-approval mechanisms, not just the remembered one, or a caller
    that explicitly asks for a one-off prompt could be silently waved
    through by an unrelated task grant."""
    app.permissions.grant_task("task-1")
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    with pytest.raises(ConfirmationDeclined):
        await app.permissions.require("click_page_element", RiskLevel.MEDIUM, "click a button",
                                      consequential=False, task_id="task-1",
                                      allow_session_grant=False)


async def test_registry_stamps_a_stable_prefix_on_a_declined_call(app, ctx):
    """registry.call() must expose *why* a call failed as a machine-readable
    prefix on ToolResult.error — recovery.py matches on this, not on the
    human-facing wording, which differs between a timeout and an explicit
    "no"."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    result = await app.deps.registry.call("close_application", {"name": "Finder"}, ctx)
    assert result.ok is False
    assert (result.error or "").startswith("confirmation_declined:")
