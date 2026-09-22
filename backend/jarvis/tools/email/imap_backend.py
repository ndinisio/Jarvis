"""IMAP/SMTP mail driver — a second :class:`MailBackend` for an account
that isn't (or can't be) in Apple Mail.

Uses only Python's standard library (``imaplib``, ``smtplib``, ``email``),
so — unlike :class:`~.mail_app.AppleMailBackend` — it works on any
platform, not only macOS. The tools and capability above it are completely
unchanged; only which backend ``_MailTool`` constructs differs (see
``tools/email/tools.py``).

Each call opens its own connection and closes it when done, mirroring how
the Apple Mail driver treats every AppleScript call as one self-contained
round trip rather than holding a session open. Reads always use IMAP UIDs,
never sequence numbers, since ``MailMessage.id`` has to remain usable by a
*later*, independent call (``body(message_id)``) — sequence numbers can
shift between connections; UIDs are stable for the mailbox's lifetime.

Known limitations, stated plainly rather than silently:

* Gmail and Outlook / Microsoft 365 have both largely moved off plain
  password auth for IMAP/SMTP in favour of OAuth. A Gmail App Password
  (still free, still plain-password IMAP underneath) works fine here; a
  full OAuth flow for an account that requires one does not exist yet.
* ``search()`` sends a plain IMAP SEARCH with no ``CHARSET`` negotiation,
  which is US-ASCII by default per RFC 3501 — a query containing non-ASCII
  text may not match on every server. Adding CHARSET support properly
  means handling a server that rejects the requested charset too, which
  felt like more than this version needed for its common case (an ASCII
  search phrase).
"""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.utils import formatdate
from typing import Any

from ...core.errors import CapabilityUnavailable, ToolError
from ...core.logging import get_logger
from .mail_app import Draft, MailBackend, MailMessage

log = get_logger("jarvis.tools.email.imap")


class ImapMailBackend(MailBackend):
    name = "IMAP"

    def __init__(self, config: Any):
        self._config = config

    # -- connection ------------------------------------------------------
    def _require_config(self) -> None:
        c = self._config
        missing = [name for name, value in (
            ("imap_host", c.imap_host), ("smtp_host", c.smtp_host),
            ("username", c.username), ("password", c.password),
        ) if not value]
        if missing:
            raise CapabilityUnavailable(
                "IMAP email isn't configured yet — set " + ", ".join(missing) +
                " (the password comes from the JARVIS_EMAIL_PASSWORD environment "
                "variable only, never the config file).",
            )

    def _connect_imap(self) -> imaplib.IMAP4_SSL:
        self._require_config()
        c = self._config
        try:
            conn = imaplib.IMAP4_SSL(c.imap_host, c.imap_port, timeout=15)
            conn.login(c.username, c.password)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise CapabilityUnavailable(
                "Couldn't reach the IMAP server — check the host, username and password.",
                detail=str(exc),
            ) from exc
        return conn

    async def available(self) -> tuple[bool, str]:
        try:
            await asyncio.to_thread(self._probe)
        except CapabilityUnavailable as exc:
            return False, exc.user_message
        return True, "ok"

    def _probe(self) -> None:
        conn = self._connect_imap()
        conn.logout()

    # -- reading -----------------------------------------------------------
    async def unread_count(self) -> int:
        uids = await asyncio.to_thread(self._search_uids, "UNSEEN")
        return len(uids)

    async def recent(self, limit: int = 8, unread_only: bool = True) -> list[MailMessage]:
        criteria = "UNSEEN" if unread_only else "ALL"
        return await asyncio.to_thread(self._fetch_recent, criteria, limit)

    async def search(self, query: str, limit: int = 10) -> list[MailMessage]:
        criteria = f'SUBJECT "{_esc(query)}"'
        return await asyncio.to_thread(self._fetch_recent, criteria, limit)

    def _search_uids(self, criteria: str) -> list[int]:
        conn = self._connect_imap()
        try:
            conn.select("INBOX", readonly=True)
            status, data = conn.uid("search", None, criteria)
            if status != "OK" or not data or not data[0]:
                return []
            # SEARCH order isn't guaranteed by the protocol; UIDs are
            # monotonically assigned, so sorting them is what actually
            # guarantees "newest first" rather than trusting server order.
            return sorted((int(u) for u in data[0].split()), reverse=True)
        finally:
            conn.logout()

    def _fetch_recent(self, criteria: str, limit: int) -> list[MailMessage]:
        conn = self._connect_imap()
        try:
            conn.select("INBOX", readonly=True)
            status, data = conn.uid("search", None, criteria)
            if status != "OK" or not data or not data[0]:
                return []
            uids = sorted((int(u) for u in data[0].split()), reverse=True)[:limit]
            messages = []
            for uid in uids:
                message = self._fetch_one(conn, uid, preview_only=True)
                if message:
                    messages.append(message)
            return messages
        finally:
            conn.logout()

    def _fetch_one(self, conn: imaplib.IMAP4_SSL, uid: int, *,
                   preview_only: bool) -> MailMessage | None:
        status, data = conn.uid("fetch", str(uid), "(FLAGS RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        flags = imaplib.ParseFlags(data[0][0])
        parsed = BytesParser().parsebytes(data[0][1])
        text = _plain_text(parsed)
        return MailMessage(
            id=str(uid),
            subject=_decode(parsed.get("Subject", "")) or "(no subject)",
            sender=_decode(parsed.get("From", "")),
            date=parsed.get("Date", ""),
            preview=" ".join(text.split())[:300] if preview_only else "",
            unread=b"\\Seen" not in flags,
            body="" if preview_only else text,
        )

    async def body(self, message_id: str) -> str:
        try:
            uid = int(message_id)
        except ValueError:
            return ""
        message = await asyncio.to_thread(self._fetch_body, uid)
        return message.body if message else ""

    def _fetch_body(self, uid: int) -> MailMessage | None:
        conn = self._connect_imap()
        try:
            conn.select("INBOX", readonly=True)
            return self._fetch_one(conn, uid, preview_only=False)
        finally:
            conn.logout()

    # -- writing: a draft via IMAP APPEND, sending via SMTP -----------------
    async def create_draft(self, draft: Draft) -> bool:
        message = _build_message(draft, self._config.username)
        await asyncio.to_thread(self._append, message, self._config.drafts_mailbox, r"\Draft")
        return True

    async def send(self, draft: Draft) -> bool:
        message = _build_message(draft, self._config.username)
        await asyncio.to_thread(self._smtp_send, message)
        try:
            await asyncio.to_thread(self._append, message, self._config.sent_mailbox, r"\Seen")
        except Exception as exc:
            # The email genuinely went out over SMTP; failing to also file a
            # local copy in Sent is a real but non-fatal gap, not a reason
            # to tell the user sending itself failed.
            log.debug("couldn't file a Sent copy after a successful send: %s", exc)
        return True

    def _append(self, message: EmailMessage, mailbox: str, flag: str) -> None:
        conn = self._connect_imap()
        try:
            status, _ = conn.append(mailbox, flag, None, message.as_bytes())
            if status != "OK":
                raise ToolError(f"The IMAP server rejected saving to {mailbox}.")
        finally:
            conn.logout()

    def _smtp_send(self, message: EmailMessage) -> None:
        self._require_config()
        c = self._config
        try:
            with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15) as conn:
                conn.starttls()
                conn.login(c.username, c.password)
                conn.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise ToolError("Couldn't send that email.", detail=str(exc)) from exc


def _esc(text: str) -> str:
    """Escape a phrase for embedding in an IMAP quoted-string search key
    (RFC 3501) — the same backslash-then-quote convention used for an
    AppleScript string literal everywhere else in this package."""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def _decode(raw: str) -> str:
    """RFC 2047 header decoding — a subject or sender containing non-ASCII
    text arrives as e.g. "=?UTF-8?B?...?=", not the readable string."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def _plain_text(message: Any) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return _decode_body(part)
        return ""
    if message.get_content_type() == "text/plain":
        return _decode_body(message)
    return ""


def _decode_body(part: Any) -> str:
    try:
        payload = part.get_payload(decode=True)
    except (LookupError, ValueError):
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _build_message(draft: Draft, from_addr: str) -> EmailMessage:
    message = MIMEMultipart()
    message["From"] = from_addr
    message["To"] = ", ".join(draft.to)
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    message["Subject"] = draft.subject
    message["Date"] = formatdate(localtime=True)
    message.attach(MIMEText(draft.body, "plain", "utf-8"))
    return message
