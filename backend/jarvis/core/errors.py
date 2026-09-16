"""Typed errors.

Every subsystem raises a :class:`JarvisError` subclass carrying a *user
message* (calm, British, no stack traces) and an optional *detail* string that
is only ever shown in developer mode.
"""

from __future__ import annotations


class JarvisError(Exception):
    """Base class for all recoverable JARVIS failures."""

    code = "error"
    user_message = "Something went wrong, sir."

    def __init__(self, user_message: str | None = None, detail: str | None = None):
        self.user_message = user_message or self.__class__.user_message
        self.detail = detail
        super().__init__(self.user_message if detail is None else f"{self.user_message} ({detail})")

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.user_message, "detail": self.detail}


class ModelUnavailable(JarvisError):
    code = "model_unavailable"
    user_message = "The local AI service isn't available."


class ModelTimeout(JarvisError):
    code = "model_timeout"
    user_message = "The model took too long to respond."


class ToolError(JarvisError):
    code = "tool_error"
    user_message = "That operation didn't complete."


class PermissionDenied(JarvisError):
    code = "permission_denied"
    user_message = "I'm not permitted to do that without your confirmation."


class ConfirmationRequired(JarvisError):
    code = "confirmation_required"
    user_message = "That action needs your confirmation."


class ConfirmationDeclined(JarvisError):
    code = "confirmation_declined"
    user_message = "Understood — I've left it alone."


class SandboxViolation(JarvisError):
    code = "sandbox_violation"
    user_message = "That path is outside the area I'm allowed to work in."


class CapabilityUnavailable(JarvisError):
    code = "capability_unavailable"
    user_message = "That capability isn't available on this machine."


class NetworkUnavailable(JarvisError):
    code = "network_unavailable"
    user_message = "That requires an internet connection, which I don't currently have."


class Cancelled(JarvisError):
    code = "cancelled"
    user_message = "Stopped, sir."
