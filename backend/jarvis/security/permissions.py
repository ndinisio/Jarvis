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
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.config import ConfigStore
from ..core.errors import ConfirmationDeclined, PermissionDenied
from ..core.events import EventBus, EventType
from ..core.logging import get_logger

log = get_logger("jarvis.security")


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

    @property
    def _config(self):
        return self._config_store.current.security

    # -- policy ------------------------------------------------------------
    def policy_for(self, risk: str) -> str:
        """Return ``"allow"`` or ``"confirm"`` for a risk level."""
        security = self._config
        if risk in security.always_confirm:
            return "confirm"
        if risk in security.auto_approve:
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
    ) -> bool:
        """Authorise *action*, asking the user when policy demands it.

        Raises :class:`ConfirmationDeclined` if the user says no or does not
        answer within the configured window.
        """
        details = details or {}
        if self.policy_for(risk) == "allow":
            return True
        if allow_session_grant and action in self._session_grants:
            log.debug("session grant covers %s", action)
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

        timeout = self._config_store.current.security.confirmation_timeout_s
        try:
            approved, remember = await asyncio.wait_for(confirmation.future, timeout=timeout)
        except asyncio.TimeoutError:
            self._bus.publish(
                EventType.CONFIRM_RESOLVED, id=confirmation.id, approved=False, reason="timeout"
            )
            raise ConfirmationDeclined(
                "I didn't receive confirmation, so I've left it.", detail=f"{action} timed out"
            )
        finally:
            self._pending.pop(confirmation.id, None)

        if approved and remember:
            self._session_grants.add(action)
        if not approved:
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
        return False, f"`{binary}` isn't on the approved list"

    def grant_session(self, action: str) -> None:
        self._session_grants.add(action)

    def revoke_session_grants(self) -> None:
        self._session_grants.clear()
