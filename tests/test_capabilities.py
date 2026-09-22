"""Capabilities: email, calendar, screen, diagnostics and conversation."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from jarvis.capabilities.base import Request
from jarvis.tools.calendar.calendar_app import (
    AppleCalendarBackend,
    CalendarEvent,
    _parse_applescript_date,
    _parse_events,
)
from jarvis.tools.contacts.contacts_app import AppleContactsBackend, Contact, _parse_contacts
from jarvis.tools.email.mail_app import (
    AppleMailBackend,
    Draft,
    MailMessage,
    _parse_messages,
    person_name,
    triage,
)
from jarvis.tools.reminders.reminders_app import AppleRemindersBackend, Reminder, _parse_reminders

FS = "\x1f"
RS = "\x1e"


# --- email ------------------------------------------------------------------

def test_mail_record_parsing():
    raw = (f"123{FS}Invoice overdue{FS}Ada <ada@example.com>{FS}Monday 5 May 2026 at 09:14:00"
           f"{FS}Please pay by Friday{FS}false{RS}")
    messages = _parse_messages(raw)
    assert len(messages) == 1
    assert messages[0].subject == "Invoice overdue"
    assert messages[0].unread is True
    assert "Please pay" in messages[0].preview


def test_sender_name_extraction():
    assert person_name("Ada Lovelace <ada@example.com>") == "Ada Lovelace"
    assert person_name("ada@example.com") == "ada"


def test_triage_is_deterministic():
    messages = [
        MailMessage(subject="Urgent: invoice overdue", preview="pay today"),
        MailMessage(subject="Lunch?", preview="fancy a sandwich"),
    ]
    triaged = triage(messages)
    assert triaged[0].urgency == "high"
    assert triaged[1].urgency == "normal"


@pytest.fixture
def mail(app, monkeypatch):
    messages = [
        MailMessage(id="1", subject="Urgent: contract deadline", sender="Ada <ada@example.com>",
                    date="Monday", preview="We need this signed today."),
        MailMessage(id="2", subject="Weekly newsletter", sender="news@example.org",
                    date="Monday", preview="This week in widgets."),
    ]

    async def unread_count(self):
        return len(messages)

    async def recent(self, limit=8, unread_only=True):
        return list(messages)

    async def body(self, message_id):
        return "Full message body."

    drafts: list[Draft] = []

    async def create_draft(self, draft):
        drafts.append(draft)
        return True

    monkeypatch.setattr(AppleMailBackend, "unread_count", unread_count)
    monkeypatch.setattr(AppleMailBackend, "recent", recent)
    monkeypatch.setattr(AppleMailBackend, "body", body)
    monkeypatch.setattr(AppleMailBackend, "create_draft", create_draft)
    monkeypatch.setattr(app.deps.registry.get("check_email").spec, "requires_macos", False)
    monkeypatch.setattr(app.deps.registry.get("draft_email").spec, "requires_macos", False)
    return drafts


async def test_email_check_summarises_and_flags_urgency(app, mail, fake_provider):
    fake_provider.responses.append("Ada needs the contract signed today. The newsletter can wait.")
    capability = app.capabilities["email"]
    response = await capability.handle(
        Request(text="check my email", args={"intent": "check"}, ctx=app.deps.tool_context())
    )
    assert "2 new messages" in response.spoken
    assert "important" in response.spoken
    assert response.display["kind"] == "email"
    assert response.display["messages"][0]["urgency"] == "high"


async def test_email_draft_never_sends(app, mail, fake_provider):
    fake_provider.json_responses.append(
        '{"to": ["ada@example.com"], "subject": "Re: contract", "body": "Signing today."}'
    )
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    capability = app.capabilities["email"]
    response = await capability.handle(
        Request(text="draft a reply to Ada saying I'll sign today", args={"intent": "draft"},
                ctx=app.deps.tool_context())
    )
    assert mail and mail[0].to == ["ada@example.com"]
    assert "Nothing has been sent" in response.text


# --- calendar ---------------------------------------------------------------

def test_applescript_date_parsing():
    parsed = _parse_applescript_date("Monday, 5 May 2026 at 14:30:00")
    assert parsed and parsed.hour == 14 and parsed.day == 5
    assert _parse_applescript_date("nonsense") is None


def test_calendar_record_parsing():
    raw = (f"Standup{FS}Monday, 5 May 2026 at 09:30:00{FS}Monday, 5 May 2026 at 09:45:00"
           f"{FS}Zoom{FS}Work{FS}false{RS}")
    events = _parse_events(raw)
    assert events[0].title == "Standup"
    assert events[0].location == "Zoom"
    assert events[0].all_day is False


def test_event_spoken_time():
    event = CalendarEvent(title="Standup", start="Monday, 5 May 2026 at 09:30:00")
    assert "9:30" in event.spoken_time()


async def test_calendar_today_is_read_not_reasoned(app, fake_provider, monkeypatch):
    today = dt.datetime.now().replace(hour=9, minute=30)

    async def events_between(self, start, end, limit=25):
        return [
            CalendarEvent(title="Standup", start=today.strftime("%A, %-d %B %Y at %H:%M:%S")),
            CalendarEvent(title="Design review",
                          start=today.replace(hour=14).strftime("%A, %-d %B %Y at %H:%M:%S")),
        ]

    monkeypatch.setattr(AppleCalendarBackend, "events_between", events_between)
    monkeypatch.setattr(app.deps.registry.get("read_calendar").spec, "requires_macos", False)

    result = await app.deps.registry.call("read_calendar", {"range": "today"},
                                          app.deps.tool_context())
    assert result.ok
    assert "2 events today" in result.summary
    assert "Standup" in result.summary
    assert fake_provider.calls == []  # answered from the calendar, not a model


# --- reminders ----------------------------------------------------------------

def test_reminder_record_parsing():
    raw = (f"Buy milk{FS}Monday, 5 May 2026 at 09:00:00{FS}2%{FS}Groceries{FS}false{RS}")
    reminders = _parse_reminders(raw)
    assert len(reminders) == 1
    assert reminders[0].title == "Buy milk"
    assert reminders[0].list_name == "Groceries"
    assert reminders[0].completed is False


async def test_list_reminders_is_read_not_reasoned(app, fake_provider, monkeypatch):
    async def list_reminders(self, list_name="", include_completed=False, limit=25):
        return [Reminder(title="Buy milk", list_name="Groceries"),
                Reminder(title="Call Ada", list_name="Personal")]

    monkeypatch.setattr(AppleRemindersBackend, "list_reminders", list_reminders)
    monkeypatch.setattr(app.deps.registry.get("list_reminders").spec, "requires_macos", False)

    result = await app.deps.registry.call("list_reminders", {}, app.deps.tool_context())
    assert result.ok
    assert "2 reminders" in result.summary
    assert "Buy milk" in result.summary
    assert fake_provider.calls == []  # answered from Reminders, not a model


async def test_create_reminder_script_is_well_formed(app):
    backend = AppleRemindersBackend(app.controller)
    captured: dict = {}

    async def capture(script, timeout=60.0):
        captured["script"] = script
        return "ok"

    backend._script = capture
    reminder = Reminder(title='Buy "milk"', due="2026-05-05T09:00", notes="2%")
    await backend.create_reminder(reminder, "Groceries")

    script = captured["script"]
    assert "make new reminder with properties" in script
    assert '\\"milk\\"' in script
    assert 'if (name of lst) is "Groceries"' in script
    assert "set time of dueDate to 32400" in script   # 09:00 in seconds


async def test_complete_reminder_reports_when_not_found(app, monkeypatch):
    async def complete_reminder(self, title, list_name=""):
        return False

    monkeypatch.setattr(AppleRemindersBackend, "complete_reminder", complete_reminder)
    monkeypatch.setattr(app.deps.registry.get("complete_reminder").spec, "requires_macos", False)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})

    result = await app.deps.registry.call("complete_reminder", {"title": "Nonexistent"},
                                          app.deps.tool_context())
    assert not result.ok
    assert "couldn't find" in result.summary.lower()


# --- contacts -----------------------------------------------------------------

def test_contact_record_parsing():
    IS = "\x1d"
    raw = f"Tom Blake{FS}tom@example.com{IS}{FS}555-1234{IS}{FS}Acme{RS}"
    contacts = _parse_contacts(raw)
    assert len(contacts) == 1
    assert contacts[0].name == "Tom Blake"
    assert contacts[0].emails == ["tom@example.com"]
    assert contacts[0].phones == ["555-1234"]


def test_contact_parsing_survives_a_comma_inside_a_value():
    """A regression test: multiple emails/phones on one contact used to be
    joined and split on a plain comma, so a phone number or note containing
    a comma (e.g. "+1 555-1234, ext. 2") would corrupt parsing. Fixed by
    using a dedicated separator distinct from any character real contact
    data would plausibly contain."""
    IS = "\x1d"
    raw = (f"Tom Blake{FS}tom@example.com{IS}tom@work.example{IS}"
          f"{FS}555-1234, ext. 2{IS}{FS}Acme{RS}")
    contacts = _parse_contacts(raw)
    assert contacts[0].emails == ["tom@example.com", "tom@work.example"]
    assert contacts[0].phones == ["555-1234, ext. 2"]


async def test_search_contacts_is_read_not_reasoned(app, fake_provider, monkeypatch):
    async def search(self, query, limit=10):
        return [Contact(name="Tom Blake", emails=["tom@example.com"], phones=["555-1234"])]

    monkeypatch.setattr(AppleContactsBackend, "search", search)
    monkeypatch.setattr(app.deps.registry.get("search_contacts").spec, "requires_macos", False)

    result = await app.deps.registry.call("search_contacts", {"query": "Tom"},
                                          app.deps.tool_context())
    assert result.ok
    assert "Tom Blake" in result.summary
    assert "tom@example.com" in result.summary
    assert fake_provider.calls == []  # answered from Contacts, not a model


async def test_search_contacts_with_no_match_says_so(app, monkeypatch):
    async def search(self, query, limit=10):
        return []

    monkeypatch.setattr(AppleContactsBackend, "search", search)
    monkeypatch.setattr(app.deps.registry.get("search_contacts").spec, "requires_macos", False)

    result = await app.deps.registry.call("search_contacts", {"query": "Nobody"},
                                          app.deps.tool_context())
    assert result.ok
    assert "don't have a contact" in result.summary.lower()


def test_a_looked_up_contact_becomes_a_person_entity_for_reference_resolution(app):
    """Feeds intelligence/entities.py's reference resolver, via the same
    generic ConversationState._absorb dispatch email already uses — no
    change to the resolver itself was needed, only teaching state.py to
    recognise the "contacts" category (see docs/extending.md's own
    "context for free" note)."""
    app.orchestrator.state.note_observation(
        "search_contacts", {"query": "brother"}, True, "Tom Blake — tom@example.com",
        {"contacts": [{"name": "Tom Blake", "emails": ["tom@example.com"], "phones": []}]},
        "contacts",
    )
    people = app.orchestrator.state.entities_of("person")
    assert any(p.label == "Tom Blake" for p in people)


async def test_list_reminders_filters_by_list_name(app, fake_provider, monkeypatch):
    captured: dict = {}

    async def list_reminders(self, list_name="", include_completed=False, limit=25):
        captured["list_name"] = list_name
        captured["include_completed"] = include_completed
        return [Reminder(title="Buy milk", list_name="Groceries")] if list_name else []

    monkeypatch.setattr(AppleRemindersBackend, "list_reminders", list_reminders)
    monkeypatch.setattr(app.deps.registry.get("list_reminders").spec, "requires_macos", False)

    result = await app.deps.registry.call("list_reminders", {"list": "Groceries"},
                                          app.deps.tool_context())
    assert result.ok
    assert captured["list_name"] == "Groceries"
    assert captured["include_completed"] is False
    assert "Groceries" in result.summary


async def test_search_reminders_matches_title_and_notes(app, monkeypatch):
    async def list_reminders(self, list_name="", include_completed=False, limit=25):
        return [Reminder(title="Buy milk", notes="2%"), Reminder(title="Call Ada", notes="")]

    monkeypatch.setattr(AppleRemindersBackend, "list_reminders", list_reminders)
    monkeypatch.setattr(app.deps.registry.get("search_reminders").spec, "requires_macos", False)

    result = await app.deps.registry.call("search_reminders", {"query": "milk"},
                                          app.deps.tool_context())
    assert result.ok
    assert "1 matching reminder" in result.summary
    assert result.data["reminders"][0]["title"] == "Buy milk"


async def test_create_reminder_tool_reports_what_it_added(app, monkeypatch):
    async def create_reminder(self, reminder, list_name=""):
        return True

    monkeypatch.setattr(AppleRemindersBackend, "create_reminder", create_reminder)
    monkeypatch.setattr(app.deps.registry.get("create_reminder").spec, "requires_macos", False)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})

    result = await app.deps.registry.call(
        "create_reminder", {"title": "Buy milk", "due": "2026-05-05T09:00"},
        app.deps.tool_context(),
    )
    assert result.ok
    assert "Buy milk" in result.summary
    assert result.data["title"] == "Buy milk"


async def test_reminders_capability_completes_by_deterministic_keyword(app, fake_provider,
                                                                       monkeypatch):
    """"done"/"complete"/"finished" routes straight to complete_reminder without
    a full tool-selection model call — mirrors CalendarCapability's own
    deterministic shortcuts for "today"/"tomorrow"/"week"."""
    completed: list[str] = []

    async def complete_reminder(self, title, list_name=""):
        completed.append(title)
        return True

    monkeypatch.setattr(AppleRemindersBackend, "complete_reminder", complete_reminder)
    monkeypatch.setattr(app.deps.registry.get("complete_reminder").spec, "requires_macos", False)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    fake_provider.json_responses.append('{"title": "Buy milk"}')

    capability = app.capabilities["reminders"]
    response = await capability.handle(
        Request(text="mark buy milk as done", ctx=app.deps.tool_context())
    )
    assert completed == ["Buy milk"]
    assert "Buy milk" in response.text


async def test_reminders_capability_creates_by_deterministic_keyword(app, fake_provider,
                                                                     monkeypatch):
    async def create_reminder(self, reminder, list_name=""):
        return True

    monkeypatch.setattr(AppleRemindersBackend, "create_reminder", create_reminder)
    monkeypatch.setattr(app.deps.registry.get("create_reminder").spec, "requires_macos", False)
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    fake_provider.json_responses.append('{"title": "Call mum", "due": "", "notes": ""}')

    capability = app.capabilities["reminders"]
    response = await capability.handle(
        Request(text="remind me to call mum", ctx=app.deps.tool_context())
    )
    assert "Call mum" in response.text


async def test_contacts_capability_answers_through_the_plan_path(app, fake_provider, monkeypatch):
    async def search(self, query, limit=10):
        return [Contact(name="Tom Blake", emails=["tom@example.com"], phones=[])]

    monkeypatch.setattr(AppleContactsBackend, "search", search)
    monkeypatch.setattr(app.deps.registry.get("search_contacts").spec, "requires_macos", False)
    fake_provider.json_responses.append('{"tool": "search_contacts", "args": {"query": "Tom"}}')

    capability = app.capabilities["contacts"]
    response = await capability.handle(Request(text="what's Tom's email?", ctx=app.deps.tool_context()))
    assert "Tom Blake" in response.text
    assert "tom@example.com" in response.text


# --- screen -----------------------------------------------------------------

async def test_screen_analysis_uses_the_vision_slot(app, fake_provider, monkeypatch, tmp_path):
    from pathlib import Path

    async def fake_capture(self, path=None, **kwargs):
        target = Path(path) if path else tmp_path / "shot.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_tiny_png())
        return target

    monkeypatch.setattr(app.controller.__class__, "capture_screen", fake_capture)
    fake_provider.responses.append("A code editor is open with a failing test.")

    result = await app.deps.registry.call(
        "analyse_screen", {"question": "what is on screen?"}, app.deps.tool_context()
    )
    assert result.ok
    assert "code editor" in result.summary
    assert result.display["kind"] == "image"
    assert result.display["image"].startswith("data:image/")
    # The captured image was attached to the model request.
    assert fake_provider.calls[-1]["messages"][-1].images


async def test_screen_capture_respects_the_configuration(app):
    app.config_store.update({"security": {"allow_screen_capture": False}})
    result = await app.deps.registry.call("capture_screen", {}, app.deps.tool_context())
    assert result.ok is False
    assert "disabled" in result.summary


async def test_screen_analysis_without_a_vision_model(app, fake_provider, monkeypatch, tmp_path):
    from pathlib import Path

    async def fake_capture(self, path=None, **kwargs):
        target = Path(path) if path else tmp_path / "shot.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_tiny_png())
        return target

    monkeypatch.setattr(app.controller.__class__, "capture_screen", fake_capture)
    fake_provider.fail = True
    result = await app.deps.registry.call("analyse_screen", {}, app.deps.tool_context())
    assert result.ok is False
    assert "captured" in result.summary
    assert result.display["image"]


# --- diagnostics ------------------------------------------------------------

async def test_diagnostics_separates_observation_from_inference(app, fake_provider, monkeypatch):
    from jarvis.tools.system.diagnostics import Diagnostics

    async def collect(self, areas=None, progress=None):
        return {
            "findings": [
                {"severity": "critical", "area": "memory",
                 "observation": "Memory pressure is critical (91%).", "detail": {}},
                {"severity": "ok", "area": "storage",
                 "observation": "410 GB of disk space is free.", "detail": {}},
            ],
            "raw": {"top_cpu": [{"name": "Chrome", "cpu": 180.0, "memory": 22.0}]},
            "severity": "critical",
            "headline": "Memory pressure is critical (91%).",
        }

    monkeypatch.setattr(Diagnostics, "collect", collect)
    fake_provider.responses.append(
        "OBSERVED\nMemory pressure is critical.\n\nLIKELY CAUSE\nChrome.\n\n"
        "RECOMMENDED ACTION\n1. Quit Chrome."
    )
    capability = app.capabilities["diagnostics"]
    task = app.tasks.create("system", "diagnostics")
    response = await capability.handle(
        Request(text="why is my mac slow", ctx=app.deps.tool_context(task=task), task=task)
    )
    assert "OBSERVED" in response.text
    assert "LIKELY CAUSE" in response.text
    assert "RECOMMENDED ACTION" in response.text
    assert response.spoken.startswith("Memory pressure is critical")


async def test_diagnostics_without_a_model_still_structures_the_answer(app, fake_provider,
                                                                      monkeypatch):
    from jarvis.tools.system.diagnostics import Diagnostics

    async def collect(self, areas=None, progress=None):
        return {
            "findings": [{"severity": "warning", "area": "storage",
                          "observation": "Only 8 GB of disk space remains.", "detail": {}}],
            "raw": {}, "severity": "warning", "headline": "Only 8 GB of disk space remains.",
        }

    monkeypatch.setattr(Diagnostics, "collect", collect)
    fake_provider.fail = True
    capability = app.capabilities["diagnostics"]
    task = app.tasks.create("system", "diagnostics")
    response = await capability.handle(
        Request(text="what's wrong", ctx=app.deps.tool_context(task=task), task=task)
    )
    assert "OBSERVED" in response.text and "RECOMMENDED ACTION" in response.text
    assert "Trash" in response.text or "Storage" in response.text


# --- conversation -----------------------------------------------------------

async def test_conversation_uses_the_fast_slot_for_short_chat(app, fake_provider):
    capability = app.capabilities["conversation"]
    assert capability._choose_slot("hello there") == "fast"
    assert capability._choose_slot("explain how APFS snapshots work") == "general"


async def test_conversation_handles_a_dead_model_politely(app, fake_provider):
    fake_provider.fail = True
    capability = app.capabilities["conversation"]
    response = await capability.handle(
        Request(text="tell me about the sea", ctx=app.deps.tool_context())
    )
    assert "isn't available" in response.text
    assert response.error


def _tiny_png() -> bytes:
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


async def test_send_it_reuses_the_last_draft_and_still_confirms(app, mail, fake_provider,
                                                                monkeypatch):
    """"Send it" must reach the HIGH-risk gate, never the mail server directly."""
    sent: list = []

    async def fake_send(self, draft):
        sent.append(draft)
        return True

    from jarvis.tools.email.mail_app import AppleMailBackend

    monkeypatch.setattr(AppleMailBackend, "send", fake_send)
    monkeypatch.setattr(app.deps.registry.get("send_email").spec, "requires_macos", False)
    fake_provider.json_responses.append(
        '{"to": ["ada@example.com"], "subject": "Re: contract", "body": "Signing today."}'
    )
    # Exercises the V1.1 capability path deliberately: this test scripts one
    # model response for one planned call. The agent's route to the same gate is
    # covered in tests/test_intelligence.py.
    app.config_store.update({"security": {"auto_approve": ["low", "medium"],
                                          "confirmation_timeout_s": 0.2},
                             "intelligence": {"enabled": False}})

    # Mail work runs as a background task, so wait for it to deliver.
    result = await app.ask("draft a reply to Ada saying I will sign the contract today")
    assert result.task_id
    for _ in range(40):
        if app.orchestrator._last_draft is not None:
            break
        await asyncio.sleep(0.05)
    assert app.orchestrator._last_draft is not None
    assert app.orchestrator._last_draft["to"] == ["ada@example.com"]

    result = await app.ask("send it")
    assert sent == []  # the confirmation timed out, so nothing was sent
    assert result.text


# --- AppleScript generation --------------------------------------------------
# These scripts run on a Mac we can't reach from the test suite, so the tests
# check the things that actually break: quoting, escaping and structure.

def test_draft_script_escapes_quotes_and_newlines():
    from jarvis.tools.email.mail_app import _draft_script

    draft = Draft(to=['ada@example.com', 'ben@example.org'],
                  subject='Re: "the contract"',
                  body='Line one\nLine two with "quotes" and a \\ backslash.')
    script = _draft_script(draft, send=False)

    assert 'make new outgoing message' in script
    assert script.count('make new to recipient') == 2
    assert '\\"the contract\\"' in script            # quotes escaped
    assert '\\n' in script                            # newlines escaped for AppleScript
    assert 'save newMessage' in script and 'send newMessage' not in script
    # A raw newline inside the string literal would break the script.
    body_line = next(line for line in script.splitlines() if 'outgoing message' in line)
    assert body_line.count('"') % 2 == 0


def test_send_script_differs_only_in_the_final_verb():
    from jarvis.tools.email.mail_app import _draft_script

    draft = Draft(to=['ada@example.com'], subject='Hi', body='Hello')
    assert 'send newMessage' in _draft_script(draft, send=True)
    assert 'save newMessage' in _draft_script(draft, send=False)


async def test_calendar_event_script_is_well_formed(app):
    backend = AppleCalendarBackend(app.controller)
    captured: dict = {}

    async def capture(script, timeout=60.0):
        captured["script"] = script
        return "ok"

    backend._script = capture
    event = CalendarEvent(title='Design "review"', start="2026-05-05T14:30", end="2026-05-05T15:30",
                          location="Studio")
    await backend.create_event(event, "Work")

    script = captured["script"]
    assert 'make new event with properties' in script
    assert '\\"review\\"' in script
    assert "set year of startDate to 2026" in script
    assert "set time of startDate to 52200" in script   # 14:30 in seconds
    assert 'if (name of cal) is "Work"' in script


def test_applescript_string_escaping():
    from jarvis.tools.macos.controller import _esc

    assert _esc('say "hello"') == 'say \\"hello\\"'
    assert _esc("back\\slash") == "back\\\\slash"
    assert "\n" not in _esc("two\nlines")
