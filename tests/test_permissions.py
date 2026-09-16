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
