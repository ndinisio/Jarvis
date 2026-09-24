"""The local session token.

JARVIS's server binds to ``127.0.0.1``, but "local" isn't "safe by default":
any process on the machine can reach it, and — since JARVIS's own job is
browsing to websites — so can a page it visits, unless the server refuses
connections that don't belong to it. Without this, a page JARVIS is looking
at could open its own WebSocket to ``/ws`` and send ``confirm.response`` to
self-approve a pending consequential confirmation: the exact gate meant to
stop an unwanted purchase, send or delete.

This is the same problem Jupyter's local notebook server solved years ago,
with the same fix: a random token, generated fresh each run, that the real
UI knows and nothing else does. It travels three ways:

* in the URL :mod:`jarvis.cli` opens the browser to (``?token=...``);
* read by the frontend from ``location.search`` and attached to the
  WebSocket URL and to every ``fetch()`` (``frontend/src/lib/api.ts``);
* checked here against every WebSocket handshake and every ``/api/*`` call.

The ``Origin`` check below is a second, independent layer for the WebSocket
specifically: even a client that somehow has the token is refused if a
browser says the request comes from a page that isn't this app's own —
closing the concrete "a page JARVIS is browsing attacks JARVIS" case without
relying on the token alone.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from urllib.parse import urlparse

from .logging import get_logger

log = get_logger("jarvis.auth")

#: Query-string / header name the token travels under.
TOKEN_PARAM = "token"
TOKEN_HEADER = "x-jarvis-token"

#: A launcher that starts the server itself (``scripts/dev.sh``, the Mac app)
#: chooses the token and hands it over here, so it can open the interface
#: without reading the token back out of the server's output — and so a
#: development server that reloads on every code change keeps the same one.
TOKEN_ENV = "JARVIS_SESSION_TOKEN"

#: Shorter than this, a supplied token is too guessable to accept.
_MIN_SUPPLIED = 32


def generate_token() -> str:
    """A fresh, unguessable per-process token."""
    return secrets.token_urlsafe(32)


def session_token() -> str:
    """This run's token: the launcher's, when one supplied a strong one."""
    supplied = os.environ.get(TOKEN_ENV, "")
    if len(supplied) >= _MIN_SUPPLIED:
        return supplied
    if supplied:
        log.warning("%s is too short to be safe (%d characters, need %d); using a fresh token",
                    TOKEN_ENV, len(supplied), _MIN_SUPPLIED)
    return generate_token()


def tokens_match(given: str | None, expected: str) -> bool:
    """Constant-time comparison — a token check is exactly the kind of
    string compare that must not leak timing information about how much of
    it was right."""
    return bool(given) and hmac.compare_digest(given, expected)


class RedactToken(logging.Filter):
    """Keeps the token out of the server's per-connection log lines.

    The WebSocket carries it in its URL, and uvicorn logs each connection
    with its full path (``"WebSocket /ws?token=…" [accepted]``) — output
    that ends up pasted into bug reports. The startup banner stays the one
    place the link is shown on purpose.
    """

    _PATTERN = re.compile(rf"({TOKEN_PARAM}=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if f"{TOKEN_PARAM}=" in message:
            record.msg, record.args = self._PATTERN.sub(r"\1[redacted]", message), None
        return True


def redact_server_logs() -> None:
    """Install :class:`RedactToken` on uvicorn's connection logger (once).
    Call after uvicorn has configured logging — its configuration step
    would otherwise run over it."""
    target = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, RedactToken) for f in target.filters):
        target.addFilter(RedactToken())


def origin_allowed(origin: str | None, *, host: str, port: int,
                   trusted: tuple[str, ...] = ()) -> bool:
    """Whether a WebSocket handshake's ``Origin`` header names this server
    (or one of the *trusted* origins, e.g. the Vite dev server that proxies
    to it during development).

    No ``Origin`` header at all means allow: browsers always send one on a
    WebSocket handshake, so its absence means the caller isn't a browser
    (curl, a native app) — a case the token check alone already covers,
    and ``Origin`` has nothing meaningful to say about it either way.
    """
    if not origin:
        return True
    if origin.rstrip("/") in trusted:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost", host):
        return False
    origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return origin_port == port
