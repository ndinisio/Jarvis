"""The permission boundary.

JARVIS has real access to a real Mac, so every tool declares a risk level and
nothing above LOW executes without passing through this broker. The model never
gets to decide whether a confirmation is needed — the tool's declared risk and
the user's configuration do.

LOW     read the time, battery, system info; open an app or a URL; read clipboard
MEDIUM  write inside the workspace, modify notes, create calendar events, POST
HIGH    send email, delete files, destructive shell, install software, system config
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.config import ConfigStore
from ..core.errors import ConfirmationDeclined, PermissionDenied
from ..core.events import EventBus, EventType
from ..core.logging import get_logger

log = get_logger("jarvis.security")

#: Matches only a bare "<binary> --version"-shaped invocation — nothing else
#: about that binary is allowed through this path. Deliberately excludes
#: lowercase "-v": for python/python3 (both in the default
#: version_check_binaries list) "-v" is verbose import tracing, not a
#: version query, and with no script argument it reads from stdin and hangs
#: rather than exiting — the opposite of "a pure read with no side effect".
#: "-V" is the safe, conventional uppercase form across the covered tools.
_VERSION_FLAG = re.compile(r"^\S+\s+(--version|-V)$")


class RiskLevel:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}

    @classmethod
    def at_least(cls, level: str, minimum: str) -> bool:
        return cls.ORDER.get(level, 1) >= cls.ORDER.get(minimum, 1)


@dataclass(slots=True)
class PendingConfirmation:
    id: str
    action: str
    risk: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future | None = None


class PermissionBroker:
    """Decides — and when required, asks — whether an action may proceed."""

    def __init__(self, config_store: ConfigStore, bus: EventBus):
        self._config_store = config_store
        self._bus = bus
        self._pending: dict[str, PendingConfirmation] = {}
        #: Actions approved for the remainder of this session.
        self._session_grants: set[str] = set()
        #: Task ids whose *routine* (non-consequential) steps are
        #: pre-approved for the task's lifetime. Scoped to one task, unlike
        #: ``_session_grants`` — see :meth:`grant_task`.
        self._task_grants: set[str] = set()

    @property
    def _config(self):
        return self._config_store.current.security

    # -- policy ------------------------------------------------------------
    def policy_for(self, risk: str) -> str:
        """Return ``"allow"`` or ``"confirm"`` for a *routine* action at *risk*.

        Consequential actions and privacy consents never reach this — see
        :meth:`require`. What's left is the everyday "you asked me to do
        this, so I'm doing it" case, which the ``security.autonomy`` setting
        governs for MEDIUM risk.
        """
        security = self._config
        if risk in security.always_confirm:
            return "confirm"
        if risk in security.auto_approve:
            return "allow"
        if risk == RiskLevel.MEDIUM and security.autonomy == "consequential_only":
            return "allow"
        return "confirm" if RiskLevel.at_least(risk, RiskLevel.MEDIUM) else "allow"

    def pending(self) -> list[dict[str, Any]]:
        return [
            {"id": p.id, "action": p.action, "risk": p.risk, "summary": p.summary,
             "details": p.details}
            for p in self._pending.values()
        ]

    # -- the gate ----------------------------------------------------------
    async def require(
        self,
        action: str,
        risk: str,
        summary: str,
        details: dict[str, Any] | None = None,
        *,
        allow_session_grant: bool = True,
        consequential: bool = False,
        consent: bool = False,
        handoff: bool = False,
        timeout_s: float | None = None,
        task_id: str | None = None,
        record: dict[str, Any] | None = None,
    ) -> bool:
        """Authorise *action*, asking the user when policy demands it.

        *consequential* marks an action that must never be waved through by a
        pre-approval — a task grant is never consulted for it, and approving
        it never adds a session grant either, whatever *remember* the user
        sends back. Everything else about it (timeout, the confirmation
        prompt) is identical to any other gated call.

        *allow_session_grant* — despite its name, predating task grants —
        gates *every* pre-approval mechanism, not only the remembered
        session grant: a caller passing ``False`` is asking for a fresh
        confirmation this specific time, and a task grant silently covering
        it anyway would defeat that. No current caller passes ``False``, but
        the check is written so a future one that does gets what it asked for.

        *consent* marks a privacy consent (e.g. starting the background screen
        watcher): autonomy never waves it through, but, unlike a
        consequential action, the answer may be remembered for the session.

        *handoff* is not a permission at all but the same channel used to
        wait for the user: "please sign in in the JARVIS window, then say
        done". Nothing pre-approves it — no setting can do the user's part
        for them — and *timeout_s* replaces the usual confirmation window,
        because signing in takes longer than saying yes.

        *record*, when given, is filled in with how the action was allowed —
        ``{"how": "setting" | "autonomy" | "task grant" | "session grant" |
        "user"}`` — for the audit trail.

        Raises :class:`ConfirmationDeclined` if the user says no or does not
        answer within the configured window.
        """
        record = record if record is not None else {}
        details = dict(details or {})
        details["offer_remember"] = not consequential and not handoff
        if handoff:
            details["handoff"] = True
        security = self._config
        # An explicit auto_approve setting is the user's own override and is
        # honoured as written — for everything at that risk level.
        if not handoff and risk in security.auto_approve and risk not in security.always_confirm:
            record["how"] = "setting"
            return True
        # The autonomy default is not an override: it never waves through a
        # consequential action or a privacy consent — agreeing to be watched
        # is not a routine step of some other request.
        if not handoff and not consequential and not consent and self.policy_for(risk) == "allow":
            record["how"] = "autonomy"
            return True
        step_by_step = self._config.autonomy == "confirm_each_step" and not consent
        if not handoff and not consequential and allow_session_grant and not step_by_step:
            if task_id and task_id in self._task_grants:
                log.debug("task grant %s covers %s", task_id, action)
                record["how"] = "task grant"
                return True
            if action in self._session_grants:
                log.debug("session grant covers %s", action)
                record["how"] = "session grant"
                return True

        confirmation = PendingConfirmation(
            id=uuid.uuid4().hex[:12], action=action, risk=risk, summary=summary, details=details
        )
        loop = asyncio.get_running_loop()
        confirmation.future = loop.create_future()
        self._pending[confirmation.id] = confirmation

        self._bus.publish(
            EventType.CONFIRM_REQUEST,
            id=confirmation.id,
            action=action,
            risk=risk,
            summary=summary,
            details=details,
        )
        self._bus.emit_state("awaiting_confirmation", action=action)

        timeout = timeout_s if timeout_s is not None else \
            self._config_store.current.security.confirmation_timeout_s
        try:
            approved, remember = await asyncio.wait_for(confirmation.future, timeout=timeout)
        except asyncio.TimeoutError:
            self._bus.publish(
                EventType.CONFIRM_RESOLVED, id=confirmation.id, approved=False, reason="timeout"
            )
            record.update(how="user", declined=True, timed_out=True)
            raise ConfirmationDeclined(
                "I didn't receive confirmation, so I've left it.", detail=f"{action} timed out"
            )
        finally:
            self._pending.pop(confirmation.id, None)

        if approved and remember and not consequential and not handoff:
            self._session_grants.add(action)
        record["how"] = "user"
        if not approved:
            record["declined"] = True
            raise ConfirmationDeclined(detail=action)
        return True

    def resolve(self, confirmation_id: str, approved: bool, remember: bool = False) -> bool:
        confirmation = self._pending.get(confirmation_id)
        if confirmation is None or confirmation.future is None or confirmation.future.done():
            return False
        confirmation.future.set_result((approved, remember))
        self._bus.publish(
            EventType.CONFIRM_RESOLVED, id=confirmation_id, approved=approved,
            action=confirmation.action,
        )
        return True

    def withdraw(self, action: str, reason: str = "withdrawn") -> int:
        """Take back any outstanding question about *action* — answered as
        "no" — because whatever wanted it no longer does (e.g. the screen
        watcher being switched off while its consent prompt is showing)."""
        count = 0
        for confirmation in [c for c in self._pending.values() if c.action == action]:
            if confirmation.future and not confirmation.future.done():
                confirmation.future.set_result((False, False))
                count += 1
            self._pending.pop(confirmation.id, None)
            self._bus.publish(
                EventType.CONFIRM_RESOLVED, id=confirmation.id, approved=False, reason=reason
            )
        return count

    def cancel_all(self, reason: str = "cancelled") -> int:
        count = 0
        for confirmation in list(self._pending.values()):
            if confirmation.future and not confirmation.future.done():
                confirmation.future.set_result((False, False))
                count += 1
            self._bus.publish(
                EventType.CONFIRM_RESOLVED, id=confirmation.id, approved=False, reason=reason
            )
        self._pending.clear()
        return count

    # -- specific guards ---------------------------------------------------
    def check_shell(self, command: str) -> tuple[bool, str]:
        """Classify a shell command against the allow/deny lists.

        Returns ``(allowed_without_confirmation, reason)``. A command on the
        denylist is never auto-approved; it must go through :meth:`require`
        at HIGH risk.
        """
        security = self._config
        if not security.allow_shell:
            raise PermissionDenied("Shell execution is disabled in the configuration.")
        tokens = command.strip().split()
        if not tokens:
            raise PermissionDenied("Empty command.")
        binary = tokens[0].rsplit("/", 1)[-1]
        if any(marker in command for marker in (">", ">>", "|", "&&", ";", "`", "$(")):
            return False, "command contains shell control characters"
        if binary in security.shell_denylist:
            return False, f"`{binary}` is on the restricted list"
        if binary in security.shell_allowlist:
            return True, "allowlisted read-only command"
        if binary in security.version_check_binaries and _VERSION_FLAG.match(command.strip()):
            return True, "version check"
        return False, f"`{binary}` isn't on the approved list"

    def grant_session(self, action: str) -> None:
        self._session_grants.add(action)

    def revoke_session_grants(self) -> None:
        self._session_grants.clear()

    # -- task-scoped grants --------------------------------------------------
    def grant_task(self, task_id: str) -> None:
        """Pre-approve *task_id*'s routine (non-consequential) steps.

        Unlike a session grant, this covers every tool a task might call
        under one approval — a long automation run shouldn't mean a fresh
        confirmation for each of 30 routine clicks — but it is scoped to
        this one task and never consulted for a call ``require()`` was told
        is ``consequential``. Callers must pair this with
        :meth:`revoke_task` once the task ends, successfully or not.
        """
        self._task_grants.add(task_id)

    def revoke_task(self, task_id: str) -> None:
        self._task_grants.discard(task_id)
