"""Fields JARVIS never types into, whichever browser it is driving.

Signing in and paying are the user's to do. The in-page fill script
(``manifest_js._FILL_TEMPLATE``) refuses these fields itself; this is the
same rule for drivers that type with genuine keystrokes instead of a
script (JARVIS Chrome), applied to what :meth:`BrowserDriver.inspect_handle`
reports about the real element — never to what a model says it is.
"""

from __future__ import annotations

import re
from typing import Any

PASSWORD_REFUSAL = ("that is a password field — signing in is for the user to do; "
                    "ask them to take over")
CARD_REFUSAL = "that is a payment card field — JARVIS never enters payment details"

_CARD_IDENT = re.compile(r"card.?number|cardnum|cvv|cvc|security.?code|expir")


def refusal(info: dict[str, Any]) -> str:
    """Why JARVIS must not type into the inspected element, or ``""``."""
    kind = str(info.get("type") or "").lower()
    auto = str(info.get("autocomplete") or "").lower()
    ident = f"{info.get('name') or ''} {info.get('id') or ''}".lower()
    if kind == "password" or "password" in auto:
        return PASSWORD_REFUSAL
    if auto.startswith("cc-") or _CARD_IDENT.search(ident):
        return CARD_REFUSAL
    return ""
