"""What a model sees of a web page.

A page is rendered as a compact, handle-addressed listing — the same shape
whichever browser produced it::

    Page: Logitech M185 Wireless Mouse : Amazon.co.uk — https://www.amazon.co.uk/dp/B0MOUSEM185
    Showing 40 of 131 elements (visible content first).
    [jv41] select "Colour" options=Select|Grey|Blue|Red value="Select"
    [jv42] button "Add to Basket"
    [jv43] button "Buy Now"
    [jv3] link "Basket 0" → /gp/cart/view.html
    …
    Open dialog: Added to Basket …
    Page text: Logitech M185 Wireless Mouse 4.6 out of 5 stars £12.99 …

Every write action (click, fill, submit) addresses an element by the
``[handle]`` shown here, so the listing is not decoration: it is the only
way a model can act on the page, which is why it must reach the model in
full rather than as a one-line summary.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from typing import Any
from urllib.parse import urlparse

from ...core import latency
from ...security import untrusted
from . import manifest_js

#: What the page listing says when a page needs the user, not JARVIS.
SIGNAL_LINES = {
    "captcha": ("Attention: this page shows a CAPTCHA / “are you human” check. Only the user can "
                "pass it — call ask_user_to_take_over."),
    "password_field": ("Attention: this page asks for a password. You never type passwords; if "
                       "signing in is needed to continue, call ask_user_to_take_over."),
}


def render_manifest(manifest: dict[str, Any], *, text_chars: int = 900, changes: str = "") -> str:
    """The page as the model sees it. JARVIS's own lines (where it is, what
    kind of page, how much is shown) come first; everything the page itself
    says — its elements, dialogs and text — is fenced as the page's words
    (``security/untrusted.py``), with a warning outside the fence if any of
    it is addressed to an AI assistant."""
    elements = manifest.get("elements") or []
    title = untrusted.defang(str(manifest.get("title") or "untitled page"))[:120]
    lines = [f"Page: {title} — {manifest.get('url', '')}"]
    signals = manifest.get("signals") or {}
    for name, line in SIGNAL_LINES.items():
        if signals.get(name):
            lines.append(line)
    total = manifest.get("total")
    offset = int(manifest.get("offset") or 0)
    if isinstance(total, int) and total > offset + len(elements):
        lines.append(f"Showing elements {offset + 1}–{offset + len(elements)} of {total} "
                     f"(visible content first; read_page_manifest with offset={offset + len(elements)} "
                     "shows more).")
    said: list[str] = [changes] if changes else []
    for element in elements:
        said.append(render_element(element))
    for dialog in manifest.get("dialogs") or []:
        said.append(f"Open dialog: {dialog}")
    text = (manifest.get("text") or "").strip()
    if text and text_chars > 0:
        said.append(f"Page text: {text[:text_chars]}" + ("…" if len(text) > text_chars else ""))
    if said:
        content = "\n".join(said)
        note = untrusted.warning(" ".join([title, content]), "the page")
        if note:
            lines.append(note)
        lines.append(untrusted.fence(content, "the page"))
    return "\n".join(lines)


def render_element(element: dict[str, Any]) -> str:
    role = element.get("role") or "control"
    text = (element.get("text") or "").replace('"', "'")
    line = f'[{element.get("handle")}] {role} "{text}"'
    name = (element.get("name") or "").replace('"', "'")
    if name and name.lower() not in text.lower():
        line += f' name="{name}"'
    placeholder = (element.get("placeholder") or "").replace('"', "'")
    if placeholder and placeholder.lower() not in text.lower():
        line += f' placeholder="{placeholder}"'
    options = element.get("options")
    if options:
        line += " options=" + "|".join(str(o) for o in options)
    value = element.get("value")
    if value and role in {"field", "select", "combobox", "textbox", "searchbox"}:
        line += f' value="{str(value)[:80]}"'
    if "checked" in element:
        line += " (checked)" if element["checked"] else " (unchecked)"
    href = element.get("href") or ""
    if href and not href.startswith(("javascript:", "#")):
        line += f" → {_short_href(href)}"
    if element.get("visible") is False:
        line += " (below)"
    return line


def _short_href(href: str) -> str:
    parsed = urlparse(href)
    target = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path or href
    if parsed.query:
        target += f"?{parsed.query}"
    return target[:80]


class PageMemory:
    """What was on each browser's page the last time it was looked at, so
    the next look can say what changed ("New since the last look: dialog
    'Added to Basket'…") — the most useful thing a small model can be told
    after an action, and easy for it to miss in a full listing."""

    #: How many (task, browser) pairs to remember.
    LIMIT = 64

    def __init__(self) -> None:
        self._seen: dict[Any, tuple[str, set[tuple[str, str]], tuple[str, ...]]] = {}

    def changes(self, key: Any, manifest: dict[str, Any]) -> str:
        """*key* identifies who is looking — a task and its browser — so one
        task's last look never stands in for another's."""
        url = str(manifest.get("url") or "")
        elements = manifest.get("elements") or []
        current = {(str(e.get("role") or ""), str(e.get("text") or "")) for e in elements}
        dialogs = tuple(manifest.get("dialogs") or ())
        previous = self._seen.pop(key, None)
        # A page listing continued with an offset is the same look, not a new one.
        if int(manifest.get("offset") or 0) == 0:
            self._seen[key] = (url, current, dialogs)
        elif previous is not None:
            self._seen[key] = previous
        while len(self._seen) > self.LIMIT:
            self._seen.pop(next(iter(self._seen)))
        if previous is None or previous[0] != url or int(manifest.get("offset") or 0):
            return ""
        fresh = [render_element(e) for e in elements
                 if (str(e.get("role") or ""), str(e.get("text") or "")) not in previous[1]]
        new_dialogs = [d for d in dialogs if d not in previous[2]]
        if not fresh and not new_dialogs:
            return "No change since the last look."
        parts = [f"dialog “{d[:80]}”" for d in new_dialogs] + fresh[:6]
        more = len(fresh) - 6
        return "New since the last look: " + "; ".join(parts) + (f" (+{more} more)" if more > 0 else "")


async def wait_until_ready(driver, *, timeout_s: float = 6.0, settle_s: float = 0.3) -> bool:
    """Wait (bounded) for the front page to finish loading.

    The AppleScript-driven browsers return from ``open`` before the page has
    loaded, so an observation taken straight away can describe the page
    being left. ``document.readyState`` is the one signal every browser
    gives; a short settle afterwards lets client-side rendering catch up.
    """
    deadline = time.monotonic() + timeout_s
    ready = False
    silent = 0
    while time.monotonic() < deadline:
        state = (await driver.run_js(manifest_js.ready_script(), timeout=5.0)).strip()
        if state == "complete":
            ready = True
            break
        # No answer at all, repeatedly, means JavaScript is off (or nothing
        # is open) — waiting longer won't change that.
        silent = silent + 1 if not state else 0
        if silent >= 3:
            return False
        await asyncio.sleep(0.25)
    if settle_s:
        await asyncio.sleep(settle_s)
    return ready


#: How long the network must have been idle, on top of the page being
#: still, after any request: a render often follows its data by a beat.
NETWORK_QUIET_S = 0.25
#: How long a page must be still when it can show it has nothing queued —
#: no short timer pending, no animation running, no request in flight
#: (JARVIS Chrome counts these; see ``manifest_js.PENDING_WORK_INIT``).
IDLE_QUIET_S = 0.25
#: A full settle this recent still holds when nothing has acted on the page
#: since and it hasn't changed at all.
REUSE_S = 1.5
_settled: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def wait_until_quiet(driver, *, quiet_s: float = 0.5, timeout_s: float = 4.0,
                           network_idle=None) -> bool:
    """Wait (bounded) until the page stops changing.

    A single-page app re-renders after its data arrives — often a few
    hundred milliseconds after the click that asked for it. Acting in that
    window types into a field that is about to be replaced, and observing in
    it shows the page as it was. So before JARVIS acts, and before it looks,
    the page must have been still for ``quiet_s``: the mutation-counting
    signature from :func:`manifest_js.signature_script` unchanged across
    polls that long. With ``network_idle`` (seconds since the browser last
    had a request in flight) the network must also have been idle for
    ``quiet_s`` plus :data:`NETWORK_QUIET_S` — so a render that follows its
    data by half a second is still waited for. A page that shows it has
    nothing queued (no pending timer or animation, the network idle) needs
    only :data:`IDLE_QUIET_S` of stillness. Returns whether it settled
    before the timeout.
    """
    return bool(await _quiet(driver, quiet_s=quiet_s, timeout_s=timeout_s, network_idle=network_idle))


async def _quiet(driver, *, quiet_s: float, timeout_s: float, network_idle=None) -> str:
    """The page's signature once it has been still long enough, or ""."""
    deadline = time.monotonic() + timeout_s
    last = None
    stable_since = time.monotonic()
    silent = 0
    while time.monotonic() < deadline:
        raw = (await driver.run_js(manifest_js.signature_script(), timeout=5.0)).strip()
        silent = silent + 1 if not raw else 0
        if silent >= 3:
            return ""
        signature, _, queued = raw.partition("|")
        now = time.monotonic()
        if signature != last:
            last, stable_since = signature, now
        elif signature.startswith("complete"):
            still = now - stable_since
            idle = network_idle() if network_idle is not None else None
            if idle is not None and queued == "0" and idle >= NETWORK_QUIET_S and still >= IDLE_QUIET_S:
                return signature                          # finished, and shows it
            if still >= quiet_s and (idle is None or idle >= quiet_s + NETWORK_QUIET_S):
                return signature
        await asyncio.sleep(0.06)
    return ""


def acted(driver=None) -> None:
    """Something was done to the page (or, with no driver, possibly to any
    page — a key pressed at the system level, a link opened): the next
    settle waits in full."""
    if driver is None:
        _settled.clear()
        return
    with contextlib.suppress(TypeError):
        _settled.pop(driver, None)


async def _still_settled(driver) -> bool:
    """Nothing has acted on the page since its last full settle, moments
    ago, and nothing about it has changed — not a node, not a request."""
    try:
        record = _settled.get(driver)
    except TypeError:
        return False
    if record is None:
        return False
    at, signature = record
    since = time.monotonic() - at
    if since > REUSE_S:
        return False
    network_idle = getattr(driver, "network_idle_s", None)
    if network_idle is not None and network_idle() < since:
        return False                                   # a request since then
    now = (await driver.run_js(manifest_js.signature_script(), timeout=5.0)).strip()
    return now.partition("|")[0] == signature


async def settle(driver) -> None:
    """Loaded, finished fetching, and still — the precondition for acting on
    or reading a page. (Timed as waiting, not acting: ``core/latency.py``.)

    A settle right after another, with nothing done in between — the look
    after an action's own wait, the check before the next action — returns
    at once when the page is exactly as it was."""
    with latency.waiting():
        if await _still_settled(driver):
            return
        await wait_until_ready(driver, settle_s=0.0)
        network_idle = getattr(driver, "network_idle_s", None)
        network = getattr(driver, "settle", None)
        if network_idle is None and network is not None:
            await network()
        signature = await _quiet(driver, quiet_s=0.5, timeout_s=6.0, network_idle=network_idle)
        if signature:
            with contextlib.suppress(TypeError):
                _settled[driver] = (time.monotonic(), signature)


async def observe_page(driver, *, limit: int = 50, text_chars: int = 900) -> tuple[dict[str, Any], str]:
    """Wait for the page, read its manifest, and render it for a model."""
    await settle(driver)
    manifest = await driver.page_manifest(limit=limit)
    if not manifest.get("elements") and not manifest.get("url"):
        return manifest, ""
    return manifest, render_manifest(manifest, text_chars=text_chars)
