"""Classifying which tool calls must always be confirmed individually.

Routine steps of something the user asked for are allowed to just happen
(see ``security.autonomy``), and a task-scoped automation run pre-approves
its own routine steps (:meth:`PermissionBroker.grant_task`) so a 30-step
shopping errand doesn't mean 30 confirmation prompts. Some actions must
never be covered by any of that — spending money, deleting something,
sending something, running an installer, typing into a terminal — however
the surrounding work was approved.

Three independent signals decide this:

* what the tool *is* — some actions are consequential whatever their
  arguments (running an installer, sending mail);
* what the call *really* touches — for the generic click/submit/type tools
  that can reach almost anything, the target the tool resolved before
  running (:meth:`Tool.inspect`): the control's own text, id, name, link and
  form action, or the application that would receive the keystrokes;
* the model's own ``label`` for the control, as a belt-and-braces check.

The target is authoritative. A model that calls the "Buy Now" button
"Add to basket" is classified on "Buy Now". Only a control's own
identifying text is inspected, never arbitrary typed content: what someone
types into a search box says nothing about what the box does.

This is a secondary safety net, not the primary one. The primary guarantee
that JARVIS never completes a purchase is that no tool to enter payment
details or complete a checkout exists at all.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

#: Tools whose every call is consequential, whatever the arguments.
ALWAYS_CONSEQUENTIAL_TOOLS = {"run_installer", "send_email", "delete_file"}

#: Checkout/payment/destructive vocabulary in a control's identifying text.
#: Covers bare "Buy"/"Pay"/"Purchase" as well as the fuller phrases — real
#: buttons are as often labelled just "Buy" or "Buy It Now" as "Buy Now".
#: Deliberately excludes "add to basket" / "add to cart" — placing an item in
#: a basket for review is explicitly allowed to be a routine step.
_CONSEQUENTIAL_PATTERN = re.compile(
    r"\b("
    r"buy(\s*it)?(\s*now)?|purchase|order\s*now|pay(\s*now)?|"
    r"place\s*(your\s*)?order|confirm\s*(purchase|order)|complete\s*(order|purchase)|"
    r"submit\s*(payment|order)|check\s*out|checkout|proceed\s*to\s*(payment|checkout)|"
    r"delete|remove\s*permanently|send"
    r")\b",
    re.IGNORECASE,
)

#: URL paths that are a purchase or payment step in themselves.
_CONSEQUENTIAL_PATH = re.compile(
    r"(?:^|/)(?:checkout|buy|payment|pay|purchase|place-?order|order-?confirm\w*)(?:$|[/?.#_-])",
    re.IGNORECASE,
)

#: Navigation tools whose destination URL is checked against the above.
_NAVIGATION_TOOLS = {"browse_to", "open_url"}

#: Tools that deliver keyboard/mouse input to whatever app is frontmost.
_NATIVE_INPUT_TOOLS = {"type_text", "press_key", "click_element"}

#: Apps where typed input is itself a command, a credential or a system change.
SENSITIVE_APPS = {
    "terminal", "iterm", "iterm2", "warp", "alacritty", "kitty", "hyper", "wezterm", "ghostty",
    "script editor", "automator", "shortcuts", "keychain access", "1password", "1password 7",
    "bitwarden", "lastpass", "dashlane", "passwords", "console", "activity monitor",
    "disk utility", "migration assistant", "boot camp assistant",
}

#: Keys of an inspected web target that identify the control itself.
_TARGET_KEYS = ("text", "label", "value", "id", "name", "title")

#: Controls that record a choice rather than perform an action. Ticking the
#: checkbox labelled "Send the invoice" on a to-do list sends nothing; the
#: button that submits the choice is what gets judged.
_STATE_ROLES = {"checkbox", "radio", "switch", "field", "select", "option", "tab", "combobox",
                "textbox", "searchbox"}


def classify(tool_name: str, arguments: dict[str, Any], spec: Any,
             target: dict[str, Any] | None = None) -> bool:
    """True when this call must be confirmed individually, every time.

    A True result means: never eligible for a task-scoped grant, never
    eligible for a remembered session grant, never waved through by
    ``security.autonomy`` — always a fresh confirmation.
    """
    if getattr(spec, "always_confirm_individually", False):
        return True
    if tool_name in ALWAYS_CONSEQUENTIAL_TOOLS:
        return True
    if tool_name in _NAVIGATION_TOOLS and _consequential_url(arguments.get("url")):
        return True
    target = target or {}
    if tool_name in _NATIVE_INPUT_TOOLS:
        app = str(target.get("application") or "").strip().lower()
        if app in SENSITIVE_APPS:
            return True
    if target:
        records_a_choice = str(target.get("role") or "").lower() in _STATE_ROLES
        identity = " ".join(str(target.get(key) or "") for key in _TARGET_KEYS)
        if not records_a_choice and _CONSEQUENTIAL_PATTERN.search(_words(identity)):
            return True
        if not records_a_choice and _consequential_url(target.get("href")):
            return True
        if target.get("role") in {"button", "control", None, ""} and _consequential_url(target.get("action")):
            return True
        if records_a_choice:
            return False
    label = arguments.get("label")
    return isinstance(label, str) and bool(_CONSEQUENTIAL_PATTERN.search(_words(label)))


def _consequential_url(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        path = urlparse(url if "//" in url else f"//{url}").path
    except ValueError:
        return False
    return bool(_CONSEQUENTIAL_PATH.search(path))


def _words(text: str) -> str:
    """Split identifiers into words so ids like ``proceedToRetailCheckout``
    and ``buy-now-button`` read the way their visible labels do."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return re.sub(r"[._\-/]+", " ", spaced)
