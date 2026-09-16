"""The deterministic command engine.

This runs *before* any model. If a request matches one of these patterns the
answer comes from macOS or a phrasebook in a few milliseconds, and no model is
loaded, prompted or billed. Roughly two thirds of everyday assistant traffic —
greetings, time, app launching, clipboard, volume, system facts — lands here.

Patterns are intentionally tight. Anything ambiguous falls through to the
heuristic and model stages rather than guessing.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .schema import RouteDecision, RouteKind, RoutePath

Args = dict[str, Any] | Callable[[re.Match], dict[str, Any]]


@dataclass(slots=True)
class QuickCommand:
    pattern: re.Pattern
    kind: str
    name: str
    args: Args = None
    confidence: float = 0.99
    long_running: bool = False

    def build(self, match: re.Match) -> dict[str, Any]:
        if self.args is None:
            return {}
        if callable(self.args):
            return self.args(match)
        return dict(self.args)


def _c(pattern: str, kind: str, name: str, args: Args = None, confidence: float = 0.99,
       long_running: bool = False) -> QuickCommand:
    return QuickCommand(re.compile(pattern, re.I), kind, name, args, confidence, long_running)


_APP = r"(?P<app>[\w .+&'-]{2,40}?)"
_TRAIL = r"(?:\s+(?:app|application|please|now|for me))?[.!]?$"

COMMANDS: list[QuickCommand] = [
    # -- conversational control ------------------------------------------
    _c(r"^\s*(?:hey |ok |okay )?jarvis[.!?]?\s*$", RouteKind.CONTROL, "wake"),
    _c(r"^\s*(?:hi|hello|hey|yo|good morning|good afternoon|good evening|morning|evening)"
       r"(?:\s+jarvis)?[.!]?\s*$", RouteKind.CONTROL, "greeting"),
    _c(r"^\s*(?:thanks|thank you|cheers|nice one|much appreciated|ta)(?:\s+jarvis)?[.!]?\s*$",
       RouteKind.CONTROL, "thanks"),
    _c(r"^\s*(?:stop|cancel|abort|halt|nevermind|never mind|forget it|quiet|shut up|"
       r"stop that|stop it|cancel that|that's enough)[.!]?\s*$", RouteKind.CONTROL, "cancel"),
    _c(r"^\s*(?:are you (?:there|awake|online)|you there)\??\s*$", RouteKind.CONTROL, "presence"),
    _c(r"^\s*(?:goodbye|bye|good night|goodnight|that'?s all)[.!]?\s*$",
       RouteKind.CONTROL, "farewell"),
    _c(r"^\s*(?:yes|yeah|yep|go ahead|do it|confirm|send it|approved?)[.!]?\s*$",
       RouteKind.CONTROL, "affirm"),
    _c(r"^\s*(?:no|nope|don'?t|cancel it|leave it)[.!]?\s*$", RouteKind.CONTROL, "decline"),

    # -- time & date -------------------------------------------------------
    _c(r"^(?:what'?s |what is |tell me )?(?:the )?time(?: is it)?\??$|^what time is it\b.*$",
       RouteKind.TOOL, "get_time", {"field": "time"}),
    _c(r"^(?:what'?s |what is )?(?:today'?s )?(?:the )?date\??$|^what day is it\b.*$|"
       r"^what'?s the date\b.*$", RouteKind.TOOL, "get_time", {"field": "date"}),

    # -- files (before applications, so "open the file X" isn't heard as an app)
    _c(r"^(?:delete|remove|bin|trash)\s+(?:the\s+)?(?:file\s+)?(?P<path>[\w .\-/]+?)"
       r"(?:\s+from\s+(?:my\s+)?(?:workspace|files))?[.!]?$",
       RouteKind.TOOL, "delete_file", lambda m: {"path": m.group("path").strip()}),
    _c(r"^(?:read|open|show me|what'?s in)\s+(?:the\s+|my\s+)?(?:file|note)\s+"
       r"(?P<path>[\w .\-/]+?)[.!?]?$",
       RouteKind.TOOL, "read_file", lambda m: {"path": m.group("path").strip()}),
    _c(r"^(?:find|search for)\s+(?:files?|notes?)\s+(?:about|matching|containing|with)\s+"
       r"(?P<q>.+?)[.!?]?$",
       RouteKind.TOOL, "search_files", lambda m: {"query": m.group("q").strip()}),

    # -- applications ------------------------------------------------------
    _c(rf"^(?:please\s+)?(?:open|launch|start|run|fire up|bring up)\s+{_APP}{_TRAIL}",
       RouteKind.TOOL, "open_application", lambda m: {"name": m.group("app")}),
    _c(rf"^(?:please\s+)?(?:close|quit|exit|shut down|kill)\s+{_APP}{_TRAIL}",
       RouteKind.TOOL, "close_application", lambda m: {"name": m.group("app")}),
    _c(rf"^(?:switch to|focus|activate|bring)\s+{_APP}(?:\s+to the front)?{_TRAIL}",
       RouteKind.TOOL, "activate_application", lambda m: {"name": m.group("app")}),
    _c(r"^(?:what apps are|which apps are|what'?s) (?:currently )?(?:running|open)\??$",
       RouteKind.TOOL, "list_applications", {"running_only": True}),
    _c(r"^(?:what apps (?:do i have|are) installed|list (?:my )?(?:installed )?apps)\??$",
       RouteKind.TOOL, "list_applications", {}),

    # -- browser -----------------------------------------------------------
    _c(r"^(?:go to|open|visit|take me to|navigate to)\s+(?:the )?(?:website\s+)?"
       r"(?P<url>(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?)[.!]?$",
       RouteKind.TOOL, "browse_to", lambda m: {"url": m.group("url")}),
    _c(r"^(?:search (?:the web |the internet |online )?for|google|look up on the web|"
       r"web search(?: for)?)\s+(?P<q>.+?)[.!?]?$",
       RouteKind.TOOL, "browse_to", lambda m: {"query": m.group("q")}),
    _c(r"^(?:what page am i on|what'?s this page|what am i reading)\??$",
       RouteKind.TOOL, "get_current_page", {"include_text": False}),

    # -- clipboard ---------------------------------------------------------
    _c(r"^(?:what did i (?:just )?copy|what'?s (?:on|in) (?:my |the )?clipboard|"
       r"read (?:me )?(?:my |the )?clipboard|what'?s copied)\??[.!]?$",
       RouteKind.TOOL, "read_clipboard", {}),
    _c(r"^(?:copy|put)\s+(?P<text>.+?)\s+(?:to|on|in|into)\s+(?:my |the )?clipboard[.!]?$",
       RouteKind.TOOL, "write_clipboard", lambda m: {"text": m.group("text")}),

    # -- screen ------------------------------------------------------------
    _c(r"^(?:take|grab|capture)(?: a| the)? (?:screenshot|screen ?grab|screen capture)[.!]?$",
       RouteKind.TOOL, "capture_screen", {}),
    _c(r"^(?:what'?s on (?:my |the )?screen|what am i looking at|what do you see|"
       r"can you see (?:what i'?m doing|my screen|this)|look at (?:my |the )?screen|"
       r"read (?:my |the )?screen)\??[.!]?$",
       RouteKind.TOOL, "analyse_screen", {"question": "Describe what is on this screen."},
       0.97, True),
    _c(r"^(?:what does this (?:error|message|dialog|warning) mean)\??$",
       RouteKind.TOOL, "analyse_screen",
       {"question": "There is an error or dialog on screen. Read it exactly and explain what it means."},
       0.97, True),

    # -- system facts ------------------------------------------------------
    _c(r"(?:how much (?:disk |storage |space )|storage (?:space )?(?:left|free|available)|"
       r"disk space|free space|space (?:left|remaining))", RouteKind.TOOL, "get_storage", {}),
    _c(r"(?:battery (?:level|percentage|percent|status|health)?|how'?s (?:my |the )?battery|"
       r"how much battery|charge(?:d)? (?:level|is the battery))",
       RouteKind.TOOL, "get_battery", {}),
    _c(r"(?:how much (?:ram|memory)|memory (?:pressure|usage|used)|ram do i have)",
       RouteKind.TOOL, "get_memory", {}),
    _c(r"(?:what (?:chip|processor|cpu) (?:does this|do i)|which chip|what kind of mac)",
       RouteKind.TOOL, "get_system_info", {"field": "chip"}),
    _c(r"(?:what (?:version of )?(?:macos|os|mac os)|which macos|macos version)",
       RouteKind.TOOL, "get_system_info", {"field": "os"}),
    _c(r"(?:how long has (?:this|my) mac been (?:on|up)|what'?s (?:my |the )?uptime|system uptime)",
       RouteKind.TOOL, "get_system_info", {"field": "uptime"}),
    _c(r"^(?:system info(?:rmation)?|tell me about (?:this|my) mac|mac specs?|"
       r"what are my specs)\??$", RouteKind.TOOL, "get_system_info", {}),
    _c(r"(?:cpu (?:usage|load)|what'?s using (?:my |the )?cpu|how busy is (?:my |the )?(?:cpu|mac))",
       RouteKind.TOOL, "get_cpu", {}),
    _c(r"(?:am i (?:online|connected)|what (?:wi-?fi|network) am i on|network status|"
       r"is (?:the )?(?:internet|wifi|wi-fi) (?:working|up|on))", RouteKind.TOOL, "get_network", {}),
    _c(r"^(?:what'?s (?:using|eating) (?:my |the )?(?:cpu|memory|ram)|"
       r"(?:show|list) (?:me )?(?:the )?(?:top |heaviest )?processes)\??$",
       RouteKind.TOOL, "get_processes", {}),

    # -- volume ------------------------------------------------------------
    _c(r"^(?:mute|silence)(?: the)?(?: sound| volume| audio)?[.!]?$",
       RouteKind.TOOL, "set_volume", {"action": "mute"}),
    _c(r"^(?:unmute|restore sound)(?: the)?(?: sound| volume| audio)?[.!]?$",
       RouteKind.TOOL, "set_volume", {"action": "unmute"}),
    _c(r"(?:set|turn|put) (?:the )?volume (?:to|at) (?P<level>\d{1,3})\s*(?:percent|%)?",
       RouteKind.TOOL, "set_volume", lambda m: {"level": int(m.group("level")), "action": "set"}),
    _c(r"(?:turn (?:the )?volume (?P<dir>up|down)|volume (?P<dir2>up|down))",
       RouteKind.CAPABILITY, "system",
       lambda m: {"intent": "volume_step",
                  "direction": (m.group("dir") or m.group("dir2") or "up")}),
    _c(r"^(?:what'?s (?:the )?volume|how loud)\??$", RouteKind.TOOL, "set_volume",
       {"action": "get"}),

    # -- diagnostics -------------------------------------------------------
    _c(r"(?:why is (?:my |this )?mac (?:so )?(?:slow|sluggish|laggy|freezing)|"
       r"what'?s wrong with (?:my |this )?mac|diagnos(?:e|tics)|check (?:my |the )?system|"
       r"is (?:something|anything) wrong with (?:my |this )?mac|health check)",
       RouteKind.CAPABILITY, "diagnostics", {}, 0.95, True),

    # -- email -------------------------------------------------------------
    _c(r"(?:check (?:my )?(?:e-?mail|mail|inbox)|any new (?:e-?mail|mail|messages)|"
       r"do i have (?:any )?(?:new )?(?:e-?mail|mail)|read (?:my )?(?:e-?mail|mail))",
       RouteKind.CAPABILITY, "email", {"intent": "check"}, 0.96, True),

    # -- calendar ----------------------------------------------------------
    _c(r"(?:what'?s on (?:today|my calendar|the calendar)|what'?s my schedule|"
       r"what do i have (?:on )?today|my agenda|any meetings today|what'?s happening today)",
       RouteKind.TOOL, "read_calendar", {"range": "today"}, 0.96, True),
    _c(r"(?:what'?s on tomorrow|tomorrow'?s schedule|what do i have tomorrow)",
       RouteKind.TOOL, "read_calendar", {"range": "tomorrow"}, 0.96, True),
    _c(r"(?:what'?s on this week|this week'?s schedule|what does my week look like)",
       RouteKind.TOOL, "read_calendar", {"range": "week"}, 0.96, True),

    # -- files & workspace -------------------------------------------------
    _c(r"(?:what'?s in my workspace|list (?:my )?(?:workspace|files)|show me my files)",
       RouteKind.TOOL, "list_files", {}),
    _c(r"^(?:where is my workspace|workspace info(?:rmation)?)\??$",
       RouteKind.TOOL, "workspace_info", {}),
    _c(r"^(?:make|take|write|add) a note(?: that| saying| about)?\s+(?P<text>.+)$",
       RouteKind.TOOL, "create_note", lambda m: {"content": m.group("text")}),

    # -- memory ------------------------------------------------------------
    _c(r"(?:what do you (?:remember|know) about me|what'?s in your memory|"
       r"what have you remembered)", RouteKind.CAPABILITY, "memory", {"intent": "recall"}),
    _c(r"^(?:remember|note|keep in mind)(?: that)?\s+(?P<text>.+)$",
       RouteKind.CAPABILITY, "memory", lambda m: {"intent": "remember", "text": m.group("text")}),
    _c(r"^forget(?: that| about)?\s*(?P<text>.*)$",
       RouteKind.CAPABILITY, "memory", lambda m: {"intent": "forget", "text": m.group("text")}),

    # -- research ----------------------------------------------------------
    _c(r"^(?:research|investigate|look into|dig into|find out about|compare)\s+(?P<q>.+)$",
       RouteKind.CAPABILITY, "research", lambda m: {"query": m.group("q")}, 0.95, True),
]


class QuickCommands:
    """Deterministic matcher. Returns ``None`` when nothing matches cleanly."""

    def __init__(self, commands: list[QuickCommand] | None = None):
        self._commands = commands if commands is not None else COMMANDS

    def match(self, text: str) -> RouteDecision | None:
        t0 = time.perf_counter()
        cleaned = _normalise(text)
        if not cleaned:
            return None
        for command in self._commands:
            found = command.pattern.search(cleaned)
            if not found:
                continue
            decision = RouteDecision(
                kind=command.kind,
                name=command.name,
                args=command.build(found),
                confidence=command.confidence,
                path=RoutePath.QUICK,
                reason=f"matched /{command.pattern.pattern[:48]}/",
                long_running=command.long_running,
            )
            decision.latency_ms = (time.perf_counter() - t0) * 1000.0
            return decision
        return None


_FILLERS = re.compile(
    r"^(?:hey |ok |okay |hi )?jarvis[,: ]+|^(?:um|uh|er)\b[, ]*|\bplease\b\s*$", re.I
)


def _normalise(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = _FILLERS.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned
