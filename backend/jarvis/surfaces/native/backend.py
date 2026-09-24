"""The macOS Accessibility API, through PyObjC.

Everything the native surface asks of the platform goes through
:class:`MacAXBackend` — reading attributes (batched into one round trip per
element where the API allows), performing actions, setting values, finding
applications and windows, and the Accessibility trust check. It is the one
file that imports ``ApplicationServices``/``AppKit``/``CoreFoundation``
(the ``native`` extra: ``pyobjc-framework-ApplicationServices``, which
brings Quartz and Cocoa with it), and it imports them lazily, so the rest of
JARVIS — and its tests — run anywhere.

**Verification note, stated plainly**: this repository's CI and development
container are Linux. The logic above this layer is tested against a fake
backend; these calls follow Apple's documented C API as PyObjC exposes it
(an out-parameter becomes an extra ``None`` argument and a tuple return),
but they have not been exercised on a Mac by this repository's tests. Every
call is defensive: a failure reads as "attribute missing" or "action
failed", never as a crash, and the AppleScript tools remain as a fallback.
"""

from __future__ import annotations

import contextlib
from typing import Any

#: AXValue types (HIServices AXValue.h); constant names differ across SDKs.
_POINT, _SIZE, _RECT, _RANGE, _ERROR = 1, 2, 3, 4, 5
#: How long one request to an app may take before it's given up on — a
#: hung app must not hang JARVIS.
MESSAGING_TIMEOUT_S = 1.5


class MacAXBackend:
    def __init__(self) -> None:
        import ApplicationServices as AS
        import CoreFoundation as CF

        self.AS = AS
        self.CF = CF
        self._value_type_id = AS.AXValueGetTypeID()
        self._multi = hasattr(AS, "AXUIElementCopyMultipleAttributeValues")

    @staticmethod
    def available() -> bool:
        try:
            import ApplicationServices  # noqa: F401
            import CoreFoundation  # noqa: F401
        except ImportError:
            return False
        return True

    # -- trust ------------------------------------------------------------------
    def trusted(self, prompt: bool = False) -> bool:
        """Has the user granted Accessibility to this process? With
        *prompt*, macOS shows its own "allow in System Settings" dialog."""
        try:
            if prompt:
                key = getattr(self.AS, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
                return bool(self.AS.AXIsProcessTrustedWithOptions({key: True}))
            return bool(self.AS.AXIsProcessTrusted())
        except Exception:
            return False

    # -- applications and windows --------------------------------------------------
    # NSWorkspace's lists are refreshed by notifications on the main run loop,
    # which a server process like JARVIS never spins — so they can go stale
    # (an app launched after start-up never appears). The live sources come
    # first: the system-wide accessibility element, and the window server.
    def frontmost(self) -> tuple[int, str] | None:
        try:
            system = self.AS.AXUIElementCreateSystemWide()
            error, app = self.AS.AXUIElementCopyAttributeValue(system, "AXFocusedApplication", None)
            if error == 0 and app is not None:
                error, pid = self.AS.AXUIElementGetPid(app, None)
                if error == 0 and pid:
                    return int(pid), str(self.attribute(app, "AXTitle") or self._name_of(int(pid)))
        except Exception:
            pass
        try:
            import AppKit

            app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return (int(app.processIdentifier()), str(app.localizedName() or "")) if app else None
        except Exception:
            return None

    def find_app(self, name: str) -> tuple[int, str] | None:
        """A running application by (case-insensitive, then partial) name."""
        wanted = name.strip().lower()
        if not wanted:
            return None
        candidates = self._window_owners() + self._workspace_apps()
        for exact in (True, False):
            for pid, label in candidates:
                lowered = label.lower()
                if (lowered == wanted) if exact else (wanted in lowered):
                    return pid, label
        return None

    def _window_owners(self) -> list[tuple[int, str]]:
        try:
            import Quartz

            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID) or []
        except Exception:
            return []
        seen: dict[int, str] = {}
        for window in windows:
            if int(window.get("kCGWindowLayer", 1)) != 0:
                continue
            pid = int(window.get("kCGWindowOwnerPID", 0))
            owner = str(window.get("kCGWindowOwnerName") or "")
            if pid and owner and pid not in seen:
                seen[pid] = owner
        return list(seen.items())

    def _workspace_apps(self) -> list[tuple[int, str]]:
        try:
            import AppKit

            running = list(AppKit.NSWorkspace.sharedWorkspace().runningApplications())
        except Exception:
            return []
        return [(int(a.processIdentifier()), str(a.localizedName() or "")) for a in running
                if a.activationPolicy() == 0]

    def _name_of(self, pid: int) -> str:
        return next((label for p, label in self._window_owners() if p == pid), "")

    def activate(self, pid: int) -> bool:
        app = self.application(pid)
        if self.set_attribute(app, "AXFrontmost", True):
            return True
        try:
            import AppKit

            running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if running is None:
                return False
            options = getattr(AppKit, "NSApplicationActivateIgnoringOtherApps", 1 << 1)
            return bool(running.activateWithOptions_(options))
        except Exception:
            return False

    def application(self, pid: int) -> Any:
        element = self.AS.AXUIElementCreateApplication(pid)
        with contextlib.suppress(Exception):
            self.AS.AXUIElementSetMessagingTimeout(element, MESSAGING_TIMEOUT_S)
        return element

    def windows(self, app: Any) -> list[Any]:
        return list(self.attribute(app, "AXWindows") or [])

    def front_window(self, app: Any) -> Any:
        return (self.attribute(app, "AXFocusedWindow") or self.attribute(app, "AXMainWindow")
                or next(iter(self.windows(app)), None))

    def focused_element(self, app: Any) -> Any:
        return self.attribute(app, "AXFocusedUIElement")

    def window_number(self, pid: int, title: str = "") -> int | None:
        """The CoreGraphics window id of *pid*'s frontmost on-screen window —
        what ``screencapture -l`` needs."""
        try:
            import Quartz

            options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        except Exception:
            return None
        mine = [w for w in windows if int(w.get("kCGWindowOwnerPID", -1)) == pid
                and int(w.get("kCGWindowLayer", 1)) == 0]
        if title:
            named = [w for w in mine if str(w.get("kCGWindowName") or "") == title]
            mine = named or mine
        return int(mine[0]["kCGWindowNumber"]) if mine else None

    # -- attributes -------------------------------------------------------------------
    def attribute(self, element: Any, name: str) -> Any:
        if element is None:
            return None
        try:
            error, value = self.AS.AXUIElementCopyAttributeValue(element, name, None)
        except Exception:
            return None
        return self._convert(value) if error == 0 else None

    def attributes(self, element: Any, names: tuple[str, ...]) -> dict[str, Any]:
        if element is None:
            return {}
        if self._multi:
            try:
                error, values = self.AS.AXUIElementCopyMultipleAttributeValues(
                    element, list(names), 0, None)
                if error == 0 and values is not None and len(values) == len(names):
                    return {name: self._convert(value) for name, value in zip(names, values)}
            except Exception:
                pass
        return {name: self.attribute(element, name) for name in names}

    def actions(self, element: Any) -> list[str]:
        try:
            error, names = self.AS.AXUIElementCopyActionNames(element, None)
        except Exception:
            return []
        return [str(n) for n in (names or [])] if error == 0 else []

    def perform(self, element: Any, action: str) -> bool:
        try:
            return self.AS.AXUIElementPerformAction(element, action) == 0
        except Exception:
            return False

    def set_attribute(self, element: Any, name: str, value: Any) -> bool:
        try:
            return self.AS.AXUIElementSetAttributeValue(element, name, value) == 0
        except Exception:
            return False

    # -- element identity -----------------------------------------------------------------
    def key(self, element: Any) -> int:
        try:
            return int(self.CF.CFHash(element))
        except Exception:
            return id(element)

    def same(self, a: Any, b: Any) -> bool:
        try:
            return bool(self.CF.CFEqual(a, b))
        except Exception:
            return a is b

    # -- values -------------------------------------------------------------------------------
    def _convert(self, value: Any) -> Any:
        """AXValue points/sizes become tuples; error placeholders become None;
        NSString/NSNumber/NSArray are already Python-like."""
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        try:
            if self.CF.CFGetTypeID(value) != self._value_type_id:
                return value
            kind = self.AS.AXValueGetType(value)
        except Exception:
            return value
        if kind == _ERROR:
            return None
        try:
            ok, data = self.AS.AXValueGetValue(value, kind, None)
        except Exception:
            return None
        if not ok:
            return None
        if kind == _POINT:
            return (float(data.x), float(data.y))
        if kind == _SIZE:
            return (float(data.width), float(data.height))
        if kind == _RECT:
            return (float(data.origin.x), float(data.origin.y),
                    float(data.size.width), float(data.size.height))
        if kind == _RANGE:
            return (int(data.location), int(data.length))
        return None
