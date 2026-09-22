"""ImapMailBackend: a second MailBackend for an account that isn't in
Apple Mail. There's no real IMAP/SMTP server to test against here, so
connection-level behaviour is proven against a fake imaplib.IMAP4_SSL that
returns realistically-shaped responses (verified empirically against the
real imaplib.ParseFlags — imaplib.ParseFlags(b'1 (FLAGS (\\Seen) RFC822
{n}') == (b'\\Seen',) — rather than guessed), and the pure parsing/building
functions are proven against real, in-memory email.message objects rather
than mocks.
"""

from __future__ import annotations

import imaplib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from jarvis.core.errors import CapabilityUnavailable, ToolError
from jarvis.tools.email.imap_backend import (
    ImapMailBackend,
    _build_message,
    _decode,
    _esc,
    _plain_text,
)
from jarvis.tools.email.mail_app import AppleMailBackend, Draft
from jarvis.tools.email.tools import CheckEmailTool


class _Config:
    def __init__(self, **overrides):
        self.provider = "imap"
        self.imap_host = "imap.example.com"
        self.imap_port = 993
        self.smtp_host = "smtp.example.com"
        self.smtp_port = 587
        self.username = "me@example.com"
        self.password = "hunter2"
        self.drafts_mailbox = "Drafts"
        self.sent_mailbox = "Sent"
        for key, value in overrides.items():
            setattr(self, key, value)


def _msg(subject: str, body: str, seen: bool = False) -> tuple[bytes, bool]:
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = "someone@example.com"
    message.attach(MIMEText(body, "plain", "utf-8"))
    return message.as_bytes(), seen


class _FakeImap:
    """Stands in for imaplib.IMAP4_SSL, pre-loaded with a fixed set of
    messages (uid -> (raw_bytes, seen))."""

    messages: dict[int, tuple[bytes, bool]] = {}
    instances: list[_FakeImap] = []
    fail_login = False

    def __init__(self, host, port, timeout=15):
        self.host, self.port = host, port
        self.appended: list[tuple[str, str, bytes]] = []
        _FakeImap.instances.append(self)

    def login(self, user, password):
        if self.fail_login or password != "hunter2":
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] invalid credentials")

    def select(self, mailbox, readonly=True):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            uids = " ".join(str(u) for u in self.messages).encode()
            return "OK", [uids]
        if command == "fetch":
            uid = int(args[0])
            if uid not in self.messages:
                return "OK", [None]
            raw, seen = self.messages[uid]
            flags = b"\\Seen" if seen else b""
            header = f"{uid} (FLAGS ({flags.decode()}) RFC822 {{{len(raw)}}}".encode()
            return "OK", [(header, raw), b")"]
        raise NotImplementedError(command)

    def append(self, mailbox, flags, date_time, message_bytes):
        self.appended.append((mailbox, flags, message_bytes))
        return "OK", [b"APPEND completed"]

    def logout(self):
        pass


class _FakeSMTP:
    sent: list = []
    fail_login = False

    def __init__(self, host, port, timeout=15):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, message):
        _FakeSMTP.sent.append(message)


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeImap.messages = {}
    _FakeImap.instances = []
    _FakeImap.fail_login = False
    _FakeSMTP.sent = []
    _FakeSMTP.fail_login = False
    yield


@pytest.fixture
def backend(monkeypatch) -> ImapMailBackend:
    monkeypatch.setattr("jarvis.tools.email.imap_backend.imaplib.IMAP4_SSL", _FakeImap)
    monkeypatch.setattr("jarvis.tools.email.imap_backend.smtplib.SMTP", _FakeSMTP)
    return ImapMailBackend(_Config())


# -- pure functions: header decoding, body extraction, message building ------

def test_decode_handles_rfc2047_encoded_words():
    assert _decode("=?UTF-8?B?Q2Fmw6k=?=") == "Café"
    assert _decode("Plain ASCII Subject") == "Plain ASCII Subject"
    assert _decode("") == ""


def test_decode_survives_garbage_without_raising():
    assert _decode("=?bogus-charset?B?####?=") == "=?bogus-charset?B?####?="


def test_plain_text_extracts_from_a_multipart_message_and_skips_attachments():
    message = MIMEMultipart()
    message.attach(MIMEText("Hello from the body.", "plain", "utf-8"))
    message.attach(MIMEText("<b>ignored html alternative</b>", "html", "utf-8"))
    attachment = MIMEText("not really an attachment but has a filename", "plain")
    attachment.add_header("Content-Disposition", "attachment", filename="notes.txt")
    message.attach(attachment)

    assert _plain_text(message) == "Hello from the body."


def test_plain_text_handles_a_non_multipart_plain_message():
    message = MIMEText("Just one part.", "plain", "utf-8")
    assert _plain_text(message) == "Just one part."


def test_plain_text_returns_empty_for_html_only_messages():
    message = MIMEText("<p>no plain part</p>", "html", "utf-8")
    assert _plain_text(message) == ""


def test_build_message_sets_headers_and_body():
    draft = Draft(to=["ada@example.com"], cc=["tom@example.com"], subject="Hi",
                  body="See you soon.")
    message = _build_message(draft, "me@example.com")
    assert message["From"] == "me@example.com"
    assert message["To"] == "ada@example.com"
    assert message["Cc"] == "tom@example.com"
    assert message["Subject"] == "Hi"
    assert _plain_text(message) == "See you soon."


def test_esc_escapes_quotes_and_backslashes_for_an_imap_search_key():
    assert _esc('say "hello"') == 'say \\"hello\\"'
    assert _esc("back\\slash") == "back\\\\slash"


# -- configuration guard --------------------------------------------------------

async def test_missing_configuration_reports_every_missing_field():
    backend = ImapMailBackend(_Config(imap_host="", username="", password=""))
    with pytest.raises(CapabilityUnavailable) as excinfo:
        await backend.unread_count()
    message = str(excinfo.value)
    assert "imap_host" in message and "username" in message and "password" in message
    assert "smtp_host" not in message  # only what's actually missing


# -- connection-level behaviour, against a fake imaplib -------------------------

async def test_unread_count_counts_only_search_results(backend):
    _FakeImap.messages = {1: _msg("First", "body", seen=False),
                          2: _msg("Second", "body", seen=True)}
    assert await backend.unread_count() == 2  # UNSEEN is the search criteria, not a re-filter


async def test_recent_returns_newest_uid_first_regardless_of_search_order(backend):
    _FakeImap.messages = {1: _msg("Oldest", "one"), 3: _msg("Newest", "three"),
                          2: _msg("Middle", "two")}
    messages = await backend.recent(limit=10, unread_only=False)
    assert [m.subject for m in messages] == ["Newest", "Middle", "Oldest"]


async def test_recent_respects_the_limit(backend):
    _FakeImap.messages = {uid: _msg(f"Message {uid}", "body") for uid in range(1, 6)}
    messages = await backend.recent(limit=2, unread_only=False)
    assert len(messages) == 2


async def test_recent_sets_unread_from_the_seen_flag(backend):
    _FakeImap.messages = {1: _msg("Unread one", "body", seen=False),
                          2: _msg("Read one", "body", seen=True)}
    messages = {m.subject: m for m in await backend.recent(limit=10, unread_only=False)}
    assert messages["Unread one"].unread is True
    assert messages["Read one"].unread is False


async def test_recent_preview_is_truncated_and_body_is_not_populated(backend):
    _FakeImap.messages = {1: _msg("Long", "x" * 500)}
    [message] = await backend.recent(limit=10, unread_only=False)
    assert len(message.preview) <= 300
    assert message.body == ""


async def test_body_refetches_the_full_text_by_uid(backend):
    _FakeImap.messages = {1: _msg("Subject", "x" * 500)}
    full = await backend.body("1")
    assert len(full) == 500


async def test_body_with_an_unknown_id_returns_empty_string(backend):
    assert await backend.body("999") == ""
    assert await backend.body("not-a-number") == ""


async def test_search_builds_an_escaped_subject_criteria(backend, monkeypatch):
    captured = {}
    real_uid = _FakeImap.uid

    def spying_uid(self, command, *args):
        if command == "search":
            captured["criteria"] = args[-1] if args else None
        return real_uid(self, command, *args)

    monkeypatch.setattr(_FakeImap, "uid", spying_uid)
    await backend.search('quarterly "results"', limit=5)
    assert captured["criteria"] == 'SUBJECT "quarterly \\"results\\""'


async def test_connection_failure_reports_capability_unavailable(backend):
    _FakeImap.fail_login = True
    with pytest.raises(CapabilityUnavailable):
        await backend.unread_count()


async def test_available_reports_ok_when_the_connection_succeeds(backend):
    assert await backend.available() == (True, "ok")


async def test_available_reports_the_reason_when_it_cannot_connect(backend):
    _FakeImap.fail_login = True
    ok, note = await backend.available()
    assert ok is False and note


# -- writing: draft via APPEND, sending via SMTP ---------------------------------

async def test_create_draft_appends_to_the_configured_drafts_mailbox(backend):
    import email

    await backend.create_draft(Draft(to=["ada@example.com"], subject="Hi", body="Hello"))
    [instance] = _FakeImap.instances
    [(mailbox, flags, raw)] = instance.appended
    assert mailbox == "Drafts"
    assert flags == r"\Draft"
    # The body is base64-encoded at the transport layer (ordinary MIME
    # behaviour), so it has to be parsed back rather than substring-matched
    # against the raw bytes on the wire.
    assert _plain_text(email.message_from_bytes(raw)) == "Hello"


async def test_send_delivers_over_smtp(backend):
    result = await backend.send(Draft(to=["ada@example.com"], subject="Hi", body="On my way."))
    assert result is True
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0]["To"] == "ada@example.com"


async def test_send_also_files_a_copy_in_sent(backend):
    await backend.send(Draft(to=["ada@example.com"], subject="Hi", body="On my way."))
    [instance] = _FakeImap.instances
    assert instance.appended and instance.appended[0][0] == "Sent"


async def test_send_still_succeeds_even_if_filing_the_sent_copy_fails(backend, monkeypatch):
    """The email genuinely went out over SMTP; a failure to also save a
    local copy in Sent must not be reported back as "sending failed" —
    that would be a false alarm about the one thing that actually matters."""
    def broken_append(self, mailbox, flags, date_time, message_bytes):
        raise imaplib.IMAP4.error("mailbox does not exist")

    monkeypatch.setattr(_FakeImap, "append", broken_append)
    result = await backend.send(Draft(to=["ada@example.com"], subject="Hi", body="On my way."))
    assert result is True
    assert len(_FakeSMTP.sent) == 1  # the send itself still happened


async def test_send_raises_a_plain_tool_error_when_smtp_fails(backend):
    _FakeSMTP.fail_login = True
    with pytest.raises(ToolError):
        await backend.send(Draft(to=["ada@example.com"], subject="Hi", body="hello"))


# -- _MailTool: backend selection and the requires_macos toggle -----------------

class _FakeDeps:
    def __init__(self, provider: str):
        self.config = _FakeConfig(provider)
        self.controller = object()


class _FakeConfig:
    def __init__(self, provider: str):
        self.email = _Config(provider=provider)


def test_mail_tool_picks_apple_mail_by_default_and_keeps_requires_macos():
    tool = CheckEmailTool(_FakeDeps("apple"))
    assert isinstance(tool.backend, AppleMailBackend)
    assert tool.spec.requires_macos is True


def test_mail_tool_picks_imap_and_clears_requires_macos_on_this_instance_only():
    tool = CheckEmailTool(_FakeDeps("imap"))
    assert isinstance(tool.backend, ImapMailBackend)
    assert tool.spec.requires_macos is False
    # The class-level spec — shared by every other CheckEmailTool built
    # while a provider="apple" config is in effect — must be untouched.
    assert CheckEmailTool.spec.requires_macos is True
