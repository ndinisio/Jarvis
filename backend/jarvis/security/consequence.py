"""Classifying which tool calls must always be confirmed individually.

A task-scoped automation run is allowed to pre-approve its own *routine*
mutating steps (see :meth:`PermissionBroker.grant_task`) so a 30-step
shopping errand doesn't mean 30 confirmation prompts. But some actions must
never be covered by that — or by a remembered session grant — no matter how
the task itself was approved: spending money, deleting something, sending
something, running an installer.

Two independent signals decide this: what the tool *is* (some actions are
consequential regardless of arguments — running an installer, sending mail)
and, for the generic click/submit tools that can reach almost anything on a
page or in an app, what control the call names. A payment button's own label
says "buy" or "checkout"; the text someone happens to type into a search box
does not, so only the ``label`` argument — the control's own name, as read
off the page or the accessibility tree, never arbitrary typed content — is
ever inspected.

This is a secondary safety net, not the primary one. The primary guarantee
that JARVIS never completes a purchase is that no tool to submit a payment or
complete a checkout is ever implemented at all (see the automation
capability's tool set). This classifier exists for the narrower case where a
generic click reaches a single-click "Buy Now"-style control.
"""

from __future__ import annotations

import re
from typing import Any

#: Tools whose every call is consequential, whatever the arguments.
ALWAYS_CONSEQUENTIAL_TOOLS = {"run_installer", "send_email", "delete_file"}

#: Checkout/payment/destructive vocabulary in a control's own label. Covers
#: bare "Buy"/"Pay"/"Purchase" as well as the fuller phrases — real buttons
#: are as often labelled just "Buy" or "Buy It Now" as "Buy Now", and a
#: requirement of exact adjacency ("buy" immediately followed by "now")
#: would silently miss the "It" in "Buy It Now".
#: Deliberately excludes "add to basket" / "add to cart" — placing an item in
#: a basket for review is explicitly allowed to be a routine, task-approved step.
_CONSEQUENTIAL_PATTERN = re.compile(
    r"\b("
    r"buy(\s*it)?(\s*now)?|purchase|order\s*now|pay(\s*now)?|"
    r"place\s*order|confirm\s*(purchase|order)|complete\s*(order|purchase)|"
    r"submit\s*(payment|order)|check\s*out|checkout|proceed\s*to\s*(payment|checkout)|"
    r"delete|remove\s*permanently|send"
    r")\b",
    re.IGNORECASE,
)


def classify(tool_name: str, arguments: dict[str, Any], spec: Any) -> bool:
    """True when this call must be confirmed individually, every time.

    A True result means: never eligible for a task-scoped grant, never
    eligible for a remembered session grant — always a fresh confirmation.
    """
    if getattr(spec, "always_confirm_individually", False):
        return True
    if tool_name in ALWAYS_CONSEQUENTIAL_TOOLS:
        return True
    label = arguments.get("label")
    return isinstance(label, str) and bool(_CONSEQUENTIAL_PATTERN.search(label))
