"""Real scroll-wheel event synthesis via Quartz
(``CGEventCreateScrollWheelEvent``).

``ScrollTool``'s original mechanism is key-based (Page Up/Down sent through
System Events via AppleScript) — reliable, but not a genuine scroll-wheel
event, so a view that only listens for scroll events rather than keyboard
paging (common in web content and some custom-drawn scrollable views) never
responds to it. This posts the real thing instead, for "up"/"down" only —
"top"/"bottom" stay on the key-based Home/End path in ``tools.py``, since
jumping to an edge is inherently a keyboard-navigation concept, not a
scroll gesture.

Needs ``pyobjc-framework-Quartz`` (the ``scroll`` extras group in
``pyproject.toml``) — genuinely optional: :func:`available` reports
``False`` and ``ScrollTool`` falls back to its existing key-based path when
it isn't installed, so nothing regresses on a host without it. The import
happens lazily, inside each function, both so an uninstalled dependency
never breaks importing this module and so a test can inject a fake
``Quartz`` via ``sys.modules`` without PyObjC actually being present.

**Verification note, stated plainly**: this repository has no macOS host
to run PyObjC/Quartz on, so :func:`scroll`'s actual on-screen effect —
including the direction sign below — has never been confirmed against a
real scroll gesture. What *is* tested here is that the right Quartz calls
happen, in the right order, with the right argument shapes, against a fake
Quartz module standing in for the real one. The sign follows Apple's
documented ``CGEventCreateScrollWheelEvent`` convention (positive scrolls
content up, negative scrolls down) and is isolated in one constant
specifically so it's trivial to flip if a real run shows it backwards.
"""

from __future__ import annotations

import time

#: Per CGEventCreateScrollWheelEvent's documented convention. Flip this if
#: a real Mac shows the opposite — see the verification note above.
_SIGN = {"up": 1, "down": -1}

#: Lines per synthesized event — one deliberate notch of a scroll wheel,
#: matched loosely against common automation examples rather than measured
#: against a real trackpad (see the verification note above).
_LINES_PER_EVENT = 3


def available() -> bool:
    try:
        import Quartz  # noqa: F401
    except ImportError:
        return False
    return True


def scroll(direction: str, amount: int) -> bool:
    """Synchronous — call via ``asyncio.to_thread``. *direction* is "up" or
    "down" only.

    Returns ``True`` the moment even one event has been posted
    successfully — deliberately, not only when every one of *amount*
    succeeds. ``ScrollTool`` falls back to the key-based path whenever
    this returns ``False``, and a failure partway through the loop (the
    first event posts, a later one raises) would otherwise mean *some*
    scrolling already happened before the fallback then scrolled again on
    top of it — the same double-action failure shape a real macOS runtime
    report once found in speech output (see core/orchestrator.py's
    ``_respond`` `already_streamed` guard). Returns ``False`` (never
    raises) only when nothing was posted at all: Quartz isn't importable,
    the direction is unrecognised, or even the first post failed."""
    try:
        import Quartz
    except ImportError:
        return False
    sign = _SIGN.get(direction)
    if sign is None:
        return False
    posted_any = False
    for _ in range(max(1, amount)):
        try:
            event = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 1, sign * _LINES_PER_EVENT,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except Exception:
            break
        posted_any = True
        # Consecutive events posted with no gap at all can coalesce into a
        # single scroll in some apps; a small pause keeps each one distinct.
        time.sleep(0.02)
    return posted_any
