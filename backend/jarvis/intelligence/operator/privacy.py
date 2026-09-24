"""Keeping private things on this Mac.

A slot's provider chain may put a free cloud model in front of the local
one. That is the user's choice for ordinary errands — but some things never
leave the machine, whatever the chain says: anything in
``models.cloud_exclusions`` (Mail, Messages, a password manager, a bank's
site), and the content of the user's own mail, messages, contacts, files and
clipboard.

The rule is one-way. A task is allowed the cloud only until it touches one
of those things; from then on every model call it makes is local, because
the conversation now carries what it read, and the conversation is what gets
sent.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

#: Tool categories whose results are the user's own private content.
PRIVATE_CATEGORIES = frozenset({"email", "messages", "contacts", "files", "clipboard"})

#: Arguments and result fields that name where an action happens.
_WHERE_KEYS = ("url", "app", "application", "name", "site", "domain", "browser")


class PrivacyGuard:
    def __init__(self, exclusions: list[str] | None = None):
        self._terms = [term.strip().lower() for term in (exclusions or []) if term and term.strip()]
        #: Why the cloud is off for this task, once it is.
        self.reason = ""

    @property
    def allow_cloud(self) -> bool:
        return not self.reason

    def check_text(self, text: str) -> None:
        """The request itself names something excluded ("check my Mail")."""
        if self.reason:
            return
        term = self._match(text)
        if term:
            self.reason = f"the task involves {term}"

    def check_situation(self, situation: str, private_tools: set[str] | frozenset[str]) -> None:
        """What the brief carries about the moment — the inbox in view, the
        results of recent reads. If any of that is private, so is the task."""
        if self.reason or not situation:
            return
        for line in situation.splitlines():
            stripped = line.strip().lstrip("-").strip()
            if stripped.startswith(("email:", "email ")) or stripped.startswith("messages:"):
                self.reason = "your mail is part of the context"
                return
            tool = stripped.split(" ", 1)[0]
            if tool in private_tools:
                self.reason = f"the context holds what {tool} read"
                return
        self.check_text(situation)

    def check_call(self, tool: str, category: str, arguments: dict[str, Any] | None,
                   data: Any = None) -> None:
        """An action is about to run (or has run, and *data* came back)."""
        if self.reason:
            return
        if category in PRIVATE_CATEGORIES:
            self.reason = f"{tool} reads your own {category}"
            return
        for value in _where(arguments) + _where(data):
            term = self._match(value)
            if term:
                self.reason = f"{tool} touched {term}"
                return

    def _match(self, text: str) -> str:
        lowered = (text or "").lower()
        if not lowered:
            return ""
        host = _host(lowered)
        for term in self._terms:
            if "." in term or term.isalnum() and len(term) <= 5:
                # A domain, or a short fragment like "bank": anywhere in a
                # host counts ("bankofexample.com"), and anywhere in text.
                if term in host or term in lowered:
                    return term
            elif re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered):
                return term
        return ""


def _where(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    found = []
    for key in _WHERE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            found.append(item)
    return found


def _host(text: str) -> str:
    candidate = text.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    try:
        return (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return ""
