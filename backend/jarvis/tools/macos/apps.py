"""Application discovery and name resolution.

Speech gives us "vs code", "chrome", "settings". macOS wants "Visual Studio
Code", "Google Chrome", "System Settings". This module bridges the two using
the *installed* application list (discovered natively via Spotlight) plus a
small alias table for the handful of names people never say correctly — not a
giant hard-coded catalogue.
"""

from __future__ import annotations

import difflib
import re
import time

from .controller import MacOSController

#: Spoken form → the canonical bundle name fragment to look for.
ALIASES: dict[str, str] = {
    "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "code": "Visual Studio Code",
    "chrome": "Google Chrome",
    "browser": "Safari",
    "settings": "System Settings",
    "system preferences": "System Settings",
    "preferences": "System Settings",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "activity monitor": "Activity Monitor",
    "finder": "Finder",
    "mail": "Mail",
    "email": "Mail",
    "calendar": "Calendar",
    "messages": "Messages",
    "notes": "Notes",
    "reminders": "Reminders",
    "music": "Music",
    "spotify": "Spotify",
    "slack": "Slack",
    "discord": "Discord",
    "notion": "Notion",
    "figma": "Figma",
    "photos": "Photos",
    "maps": "Maps",
    "app store": "App Store",
    "xcode": "Xcode",
    "docker": "Docker",
    "zoom": "zoom.us",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
    "teams": "Microsoft Teams",
    "obsidian": "Obsidian",
    "arc": "Arc",
    "firefox": "Firefox",
    "brave": "Brave Browser",
    "edge": "Microsoft Edge",
    "preview": "Preview",
    "quicktime": "QuickTime Player",
    "screenshot": "Screenshot",
    "calculator": "Calculator",
}

#: Quitting these would be hostile or destabilising, so JARVIS refuses.
PROTECTED = {
    "finder", "systemuiserver", "dock", "windowserver", "loginwindow", "controlcenter",
    "system settings", "system preferences", "activity monitor", "jarvis",
}


class AppCatalog:
    """Cached view of installed applications with fuzzy resolution."""

    def __init__(self, controller: MacOSController, ttl_s: float = 300.0):
        self._controller = controller
        self._ttl = ttl_s
        self._apps: list[str] = []
        self._loaded_at = 0.0

    async def apps(self, refresh: bool = False) -> list[str]:
        if refresh or not self._apps or time.time() - self._loaded_at > self._ttl:
            discovered = await self._controller.list_applications()
            if discovered:
                self._apps = discovered
                self._loaded_at = time.time()
        return self._apps

    async def resolve(self, spoken: str) -> tuple[str | None, float]:
        """Map a spoken application name to an installed application.

        Returns ``(name, confidence)``; ``name`` is ``None`` when nothing
        plausible is installed.
        """
        query = _normalise(spoken)
        if not query:
            return None, 0.0
        apps = await self.apps()
        if not apps:
            # Can't enumerate (Spotlight off / non-mac): trust the spoken name.
            return ALIASES.get(query, spoken.strip().title()), 0.4

        index = {_normalise(a): a for a in apps}

        if query in index:
            return index[query], 1.0

        alias = ALIASES.get(query)
        if alias:
            alias_key = _normalise(alias)
            if alias_key in index:
                return index[alias_key], 0.98
            for key, original in index.items():
                if alias_key in key:
                    return original, 0.9

        for key, original in index.items():
            if key == query or key.startswith(query + " "):
                return original, 0.95
        starts = [orig for key, orig in index.items() if key.startswith(query)]
        if starts:
            return min(starts, key=len), 0.85
        contains = [orig for key, orig in index.items() if query in key]
        if contains:
            return min(contains, key=len), 0.75

        close = difflib.get_close_matches(query, list(index), n=1, cutoff=0.72)
        if close:
            return index[close[0]], round(difflib.SequenceMatcher(None, query, close[0]).ratio(), 2)
        return None, 0.0

    @staticmethod
    def is_protected(name: str) -> bool:
        return _normalise(name) in PROTECTED

    async def suggestions(self, spoken: str, limit: int = 3) -> list[str]:
        apps = await self.apps()
        return difflib.get_close_matches(_normalise(spoken), [_normalise(a) for a in apps],
                                         n=limit, cutoff=0.4)


def _normalise(value: str) -> str:
    value = re.sub(r"\.app$", "", (value or "").strip(), flags=re.I)
    value = re.sub(r"[^\w\s.+-]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()
