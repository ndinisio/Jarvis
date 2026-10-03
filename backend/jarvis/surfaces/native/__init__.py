"""Native macOS apps as a surface JARVIS can see and act on (v3.0).

``ax`` lists a window's controls from the Accessibility tree, ``input``
posts genuine keyboard and mouse events, ``marks`` reads on-screen text and
numbers what's worth pointing at, and ``surface`` ties them together for the
app tools. ``backend`` is the only module that touches PyObjC.
"""

from .surface import PERMISSION_HINT, AmbiguousOption, NativeError, NativeSurface

__all__ = ["PERMISSION_HINT", "AmbiguousOption", "NativeError", "NativeSurface"]
