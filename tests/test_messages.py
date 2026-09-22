"""Messages: reading via chat.db (read-only), sending via Messages.app.

Unlike Calendar/Reminders/Contacts, reading here has no AppleScript to mock
— there is no AppleScript reading API at all, which is the whole reason this
driver queries chat.db directly (see messages_app.py's module docstring).
So these tests build a real, temporary SQLite database with the actual
Messages schema and query it for real, rather than mocking the backend
method the way the other drivers' tests do — that's the only way to prove
the SQL itself is correct.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from jarvis.capabilities.base import Request
from jarvis.capabilities.simple import _SEND_PATTERN
from jarvis.core.errors import CapabilityUnavailable, ToolError
from jarvis.tools.macos.controller import ShellResult
from jarvis.tools.messages.messages_app import AppleMessagesBackend, _mac_time_to_iso

_SCHEMA = """
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, is_from_me INTEGER, handle_id INTEGER
);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
"""


def _build_chat_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany("INSERT INTO handle (ROWID, id) VALUES (?, ?)",
                     [(1, "tom@example.com"), (2, "+15551234567")])
    conn.executemany("INSERT INTO chat (ROWID, chat_identifier, display_name) VALUES (?, ?, ?)",
                     [(1, "chat1", "Tom Blake")])
    # A nanosecond-scale date (modern macOS) and a legacy seconds-scale one,
    # deliberately mixed, since real databases can carry both eras of row.
    conn.executemany(
        "INSERT INTO message (ROWID, text, date, is_from_me, handle_id) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Running late, sorry!", 725000000000000000, 0, 1),
            (2, "No worries, see you soon", 725000100000000000, 1, None),
            (3, "Don't forget the milk", 700000000, 0, 2),   # legacy seconds-scale
            (4, None, 725000200000000000, 0, 1),             # rich-text only, no plain text
        ],
    )
    conn.executemany("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                     [(1, 1), (1, 2), (1, 3)])
    conn.commit()
    conn.close()


@pytest.fixture
def chat_db(tmp_path) -> Path:
    path = tmp_path / "chat.db"
    _build_chat_db(path)
    return path


@pytest.fixture
def backend(app, chat_db) -> AppleMessagesBackend:
    return AppleMessagesBackend(app.controller, db_path=chat_db)


# -- reading -------------------------------------------------------------------

def test_mac_time_conversion_handles_both_eras():
    nanosecond_scale = _mac_time_to_iso(725000000000000000)
    second_scale = _mac_time_to_iso(725000000)
    assert nanosecond_scale.startswith("2024-") or nanosecond_scale.startswith("2023-")
    assert nanosecond_scale == second_scale
    assert _mac_time_to_iso(None) == ""
    assert _mac_time_to_iso(0) == ""


async def test_mac_time_conversion_never_raises_on_a_wild_value():
    # datetime's own range tops out at year 9999 — well past any plausible
    # message date but a value this integer column could technically hold.
    far_future_nanoseconds = 10**21
    assert _mac_time_to_iso(far_future_nanoseconds) == ""


async def test_recent_orders_newest_first_and_skips_rich_text_only_rows(backend):
    messages = await backend.recent(limit=10)
    # Row 4 (text is NULL) must never appear.
    assert all(m.text for m in messages)
    assert [m.id for m in messages] == ["2", "1", "3"]  # newest date first


async def test_recent_maps_is_from_me_and_the_sender_handle(backend):
    messages = await backend.recent(limit=10)
    by_id = {m.id: m for m in messages}
    assert by_id["1"].sender == "tom@example.com" and by_id["1"].is_from_me is False
    assert by_id["2"].sender == "me" and by_id["2"].is_from_me is True


async def test_search_matches_message_text_only(backend):
    hits = await backend.search("milk")
    assert [m.id for m in hits] == ["3"]
    assert (await backend.search("nonexistent-phrase")) == []


async def test_reading_an_inaccessible_database_reports_full_disk_access(app, tmp_path):
    """A missing/unreadable chat.db (no Full Disk Access granted, or nobody
    has ever used Messages on this account) must fail with an honest,
    actionable message — not a raw sqlite traceback — and must name Full
    Disk Access specifically, since that is a different permission from the
    Automation one every other driver in this package relies on."""
    backend = AppleMessagesBackend(app.controller, db_path=tmp_path / "does-not-exist.db")
    with pytest.raises(CapabilityUnavailable, match="Full Disk Access"):
        await backend.recent()


async def test_reading_does_not_block_the_event_loop(backend):
    """The sqlite query is synchronous; recent()/search() must hand it to a
    thread rather than blocking the loop, or every other concurrent turn
    would stall while chat.db is read."""
    started = asyncio.Event()

    async def marker():
        started.set()

    task = asyncio.create_task(marker())
    await backend.recent(limit=10)
    await asyncio.sleep(0)
    assert started.is_set()
    await task


# -- sending ---------------------------------------------------------------------

async def test_send_script_tries_imessage_then_falls_back_to_sms(app, monkeypatch):
    backend = AppleMessagesBackend(app.controller)
    captured: dict = {}

    async def capture(script, timeout=30.0):
        captured["script"] = script
        return ShellResult(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(app.controller, "osascript", capture)
    await backend.send('Tom "Test" Blake', "Running late")

    script = captured["script"]
    assert "service type = iMessage" in script
    assert "service type = SMS" in script
    assert 'send "Running late" to targetBuddy' in script
    assert '\\"Test\\"' in script  # the recipient's quote was escaped


async def test_send_reports_capability_unavailable_when_not_authorized(app, monkeypatch):
    async def fake_osascript(script, timeout=30.0):
        return ShellResult(returncode=1, stdout="", stderr="not authorized to send Apple events")

    backend = AppleMessagesBackend(app.controller)
    monkeypatch.setattr(app.controller, "osascript", fake_osascript)
    with pytest.raises(CapabilityUnavailable):
        await backend.send("tom@example.com", "hello")


async def test_send_reports_a_plain_failure_for_anything_else(app, monkeypatch):
    async def fake_osascript(script, timeout=30.0):
        return ShellResult(returncode=1, stdout="", stderr="Messages got an error: no such buddy")

    backend = AppleMessagesBackend(app.controller)
    monkeypatch.setattr(app.controller, "osascript", fake_osascript)
    with pytest.raises(ToolError):
        await backend.send("nobody@example.com", "hello")


# -- tool-level: risk, confirmation wording, and the never-remembered guarantee --

async def test_send_message_tool_is_high_risk_and_always_confirms_individually(app):
    spec = app.deps.registry.get("send_message").spec
    assert spec.risk == "high"
    assert spec.always_confirm_individually is True


async def test_send_message_confirmation_template_renders_recipient_and_body(app, monkeypatch):
    from jarvis.tools.messages.messages_app import AppleMessagesBackend as Backend

    async def send(self, recipient, body):
        return True

    monkeypatch.setattr(Backend, "send", send)
    monkeypatch.setattr(app.deps.registry.get("send_message").spec, "requires_macos", False)

    async def approve_soon():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                assert pending[0]["summary"] == 'Send this to Tom: "Running late"?'
                app.permissions.resolve(pending[0]["id"], True)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no confirmation was ever requested")

    asyncio.create_task(approve_soon())
    result = await app.deps.registry.call(
        "send_message", {"recipient": "Tom", "body": "Running late"}, app.deps.tool_context()
    )
    assert result.ok


async def test_send_message_is_never_covered_by_a_remembered_session_grant(app, monkeypatch):
    """A HIGH-risk action is never rememberable in general, but this proves
    it specifically for send_message end-to-end through the real registry
    dispatch — not just that always_confirm_individually is set on the
    spec, but that setting it actually changes what permissions.require()
    does when a call goes through."""
    async def send(self, recipient, body):
        return True

    monkeypatch.setattr(AppleMessagesBackend, "send", send)
    monkeypatch.setattr(app.deps.registry.get("send_message").spec, "requires_macos", False)
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})

    async def approve_with_memory():
        for _ in range(50):
            pending = app.permissions.pending()
            if pending:
                app.permissions.resolve(pending[0]["id"], True, remember=True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_with_memory())
    first = await app.deps.registry.call(
        "send_message", {"recipient": "Tom", "body": "First"}, app.deps.tool_context()
    )
    assert first.ok

    # Nothing resolves the second call — if a session grant wrongly covered
    # it, this would succeed instantly instead of timing out.
    second = await app.deps.registry.call(
        "send_message", {"recipient": "Tom", "body": "Second"}, app.deps.tool_context()
    )
    assert not second.ok
    assert "confirmation_declined" in (second.error or "")


# -- capability: the deterministic "send" shortcut, and its false-positive guard --

@pytest.mark.parametrize("text", [
    "send Tom a text saying I'm running late",
    "text Ada to say thanks",
])
def test_send_pattern_matches_genuine_send_requests(text):
    assert _SEND_PATTERN.search(text.lower())


@pytest.mark.parametrize("text", [
    "check my messages",
    "any new texts?",
    "what does this message mean",
    "search my messages for the invoice",
])
def test_send_pattern_does_not_fire_on_reading_requests(text):
    assert not _SEND_PATTERN.search(text.lower())


async def test_messages_capability_sends_through_the_deterministic_shortcut(
    app, fake_provider, monkeypatch
):
    sent: list[dict] = []

    async def send(self, recipient, body):
        sent.append({"recipient": recipient, "body": body})
        return True

    monkeypatch.setattr(AppleMessagesBackend, "send", send)
    monkeypatch.setattr(app.deps.registry.get("send_message").spec, "requires_macos", False)
    app.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                          "always_confirm": []}})
    fake_provider.json_responses.append('{"recipient": "Tom", "body": "Running late"}')

    capability = app.capabilities["messages"]
    response = await capability.handle(
        Request(text="text Tom saying I'm running late", ctx=app.deps.tool_context())
    )
    assert sent == [{"recipient": "Tom", "body": "Running late"}]
    assert "Tom" in response.text
