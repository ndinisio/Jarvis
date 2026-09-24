"""The deterministic command engine.

This runs *before* any model. If a request matches one of these patterns the
answer comes from macOS or a phrasebook in a few milliseconds, and no model is
loaded, prompted or billed. Roughly two thirds of everyday assistant traffic —
greetings, time, app launching, clipboard, volume, system facts — lands here.

Patterns are intentionally tight. Anything ambiguous falls through to
IntentTriage (see ``intelligence/triage.py``) rather than guessing.

A syntactic match is not the same as a safe one (V1.3 F2/F3): "Open the
second one." and "Open BBC.co.uk." both satisfy the shape of "open
<application name>", but neither names a concrete, deterministic
application — the first depends on conversation state, the second is a
browser destination wearing an application-launch sentence. ``QuickCommand.safe``
is the gate for this: a regex match whose argument isn't trustworthy is
treated as no match at all, and the search continues (for a web destination,
straight into the browser pattern below; for a bare reference, all the way
through to Triage and reference resolution).

**One request, one clause (v3.0).** The fast path is for single, complete
commands. "Search for AirPods on Amazon and add them to my basket" contains
a perfectly good "search for …" but is a two-step errand; answering it with a
web search was the most common way JARVIS "misunderstood" people. A second
clause ("… and add", "…, then open", "…; also…") means the request goes to the
model that can plan it. Politeness ("could you…", "… for me please") is
stripped before matching, so ordinary phrasing still gets the fast answer,
and patterns for system facts are anchored, so "how much storage does the
iPhone 16 have" is a question, not a request for this Mac's disk space.
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
    #: An extra check on a syntactic match before it is trusted to execute
    #: deterministically. Returns ``False`` to mean "this matched the shape,
    #: but the argument is not safe to treat as complete and deterministic" —
    #: the matcher keeps looking rather than accepting it. A regex match
    #: alone was letting "the second one" become
    #: ``open_application(name="the second one")`` and "BBC.co.uk" become
    #: ``open_application(name="BBC.co.uk")`` before either IntentTriage or
    #: browse_to's own verification ever saw the request (V1.3 F2/F3).
    #: Quick routing is allowed to be fast only when it is also safe.
    safe: Callable[[re.Match], bool] | None = None

    def build(self, match: re.Match) -> dict[str, Any]:
        if self.args is None:
            return {}
        if callable(self.args):
            return self.args(match)
        return dict(self.args)

    def is_safe(self, match: re.Match) -> bool:
        return self.safe is None or self.safe(match)


def _c(pattern: str, kind: str, name: str, args: Args = None, confidence: float = 0.99,
       long_running: bool = False, safe: Callable[[re.Match], bool] | None = None) -> QuickCommand:
    return QuickCommand(re.compile(pattern, re.I), kind, name, args, confidence, long_running, safe)


_APP = r"(?P<app>[\w .+&'-]{2,40}?)"
_TRAIL = r"(?:\s+(?:app|application|please|now|for me))?[.!]?$"

#: The exact shape browse_to's own quick pattern accepts as a URL/domain —
#: kept as one fragment so the application-name safety check below can never
#: drift out of sync with what the browser pattern would actually take.
_URL_SHAPE = r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?"

#: A domain, URL or "www." prefix is a browser destination, not an
#: application name — deliberately simple pattern matching, not a general
#: URL parser, just enough to keep open/close/activate out of browse_to's way.
_WEB_DESTINATION = re.compile(rf"^(?:{_URL_SHAPE}|www\.\S+)$", re.I)

#: A bare reference to something already in context ("the second one", "that
#: one", "it") is not a deterministic application name — resolving it needs
#: conversation state, which only IntentTriage/Understanding/ReferenceResolver
#: can do. This mirrors, rather than imports, the ordinal/pronoun vocabulary
#: ReferenceResolver already uses (intelligence/entities.py): the two lists
#: are allowed to diverge slightly, since this one only has to recognise
#: "this needs interpretation", not classify what kind of thing is meant.
_BARE_REFERENCE = re.compile(
    r"^(?:the\s+)?(?:first|second|third|fourth|fifth|next|last|previous|other|latest|newest)"
    r"\s+one$"
    r"|^(?:this|that|these|those|it|them)(?:\s+one)?$",
    re.I,
)


#: Words that name a thing inside an app or on a page, never an app itself:
#: "open a new tab", "open the downloads folder", "open my last email".
_NOT_AN_APP = re.compile(
    r"\b(?:tabs?|windows?|pages?|links?|folders?|files?|e-?mails?|documents?|things?|stuff|"
    r"results?|websites?|site|url|whatever)\b"
    r"|\bi (?:was|use|am|have|had|need|want)\b"
    r"|\s(?:for|from|with|about|on|at)\s",
    re.I,
)


def _is_concrete_app_name(match: re.Match) -> bool:
    """The safety gate behind every open/close/activate-application match."""
    name = (match.group("app") or "").strip()
    return (not _BARE_REFERENCE.match(name) and not _WEB_DESTINATION.match(name)
            and not _NOT_AN_APP.search(name))


#: Sites people name when they want a search done *there*, not on the web at
#: large — "search for usb cables on amazon" is a shopping errand.
_SITE = re.compile(
    r"\b(?:on|at|in|from|via|using)\s+(?:the\s+)?(?:amazon|ebay|you ?tube|wikipedia|spotify|reddit|"
    r"imdb|google maps|maps|netflix|etsy|twitter|facebook|instagram|tiktok|linkedin|github|argos|"
    r"currys|john lewis|tesco|asos|skyscanner|booking\.com|airbnb|trainline|bbc|apple music)\b",
    re.I,
)


def _is_plain_web_search(match: re.Match) -> bool:
    return not _SITE.search(match.group("q") or "")


def _names_a_file(match: re.Match) -> bool:
    """A delete must name a file — "remove the kettle from my amazon basket"
    matched the shape of "remove <path>" and nearly became delete_file."""
    path = (match.group("path") or "").strip()
    return (bool(re.search(r"\bfile\b", match.group(0), re.I))
            or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", path)) or "/" in path)


def _is_a_fact_to_remember(match: re.Match) -> bool:
    """"Remember to buy milk" is a reminder request, not a fact about the user."""
    return not re.match(r"to\b", (match.group("text") or "").strip(), re.I)


def _volume_step(match: re.Match) -> dict[str, Any]:
    direction = next((g for g in match.groups() if g in {"up", "down"}), "up")
    return {"intent": "volume_step", "direction": direction}


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
    _c(r"^\s*(?:yes|yeah|yep|go ahead|do it|confirm|send it|approved?|"
       r"(?:ok(?:ay)?,? |all )?done|i'?m (?:all )?done|i'?ve (?:done it|finished)|finished|"
       r"i'?m (?:signed|logged) in|(?:signed|logged) in)[.!]?\s*$",
       RouteKind.CONTROL, "affirm"),
    _c(r"^\s*(?:no|nope|don'?t|cancel it|leave it)[.!]?\s*$", RouteKind.CONTROL, "decline"),

    # -- time & date -------------------------------------------------------
    _c(r"^(?:what'?s |what is |tell me )?(?:the )?time(?: is it)?(?: now)?\??$|^what time is it\??$",
       RouteKind.TOOL, "get_time", {"field": "time"}),
    _c(r"^(?:what'?s |what is )?(?:today'?s )?(?:the )?date(?: today)?\??$|^what day is it(?: today)?\??$|"
       r"^what'?s the date today\??$", RouteKind.TOOL, "get_time", {"field": "date"}),

    # -- files (before applications, so "open the file X" isn't heard as an app)
    _c(r"^(?:delete|remove|bin|trash)\s+(?:the\s+)?(?:file\s+)?(?P<path>[\w .\-/]+?)"
       r"(?:\s+from\s+(?:my\s+)?(?:workspace|files))?[.!]?$",
       RouteKind.TOOL, "delete_file", lambda m: {"path": m.group("path").strip()},
       safe=_names_a_file),
    _c(r"^(?:read|open|show me|what'?s in)\s+(?:the\s+|my\s+)?(?:file|note)\s+"
       r"(?P<path>[\w .\-/]+?)[.!?]?$",
       RouteKind.TOOL, "read_file", lambda m: {"path": m.group("path").strip()}),
    _c(r"^(?:find|search for)\s+(?:files?|notes?)\s+(?:about|matching|containing|with)\s+"
       r"(?P<q>.+?)[.!?]?$",
       RouteKind.TOOL, "search_files", lambda m: {"query": m.group("q").strip()}),

    # -- everyday: tabs, music, appearance, lock ---------------------------
    # Before applications, so "open a new tab" is a tab and never an app.
    _c(r"^(?:(?:open|pop open|fire up|make|create|start|give me|get me)\s+)?(?:a\s+)?new (?:browser )?tab"
       r"(?:\s+in\s+(?P<browser>safari|chrome|google chrome|arc|brave|edge|firefox))?$",
       RouteKind.TOOL, "browser_tab", lambda m: {"action": "new", "browser": m.group("browser") or ""}),
    _c(r"^close (?:this|the|that|current|my) tab$", RouteKind.TOOL, "browser_tab", {"action": "close"}),
    _c(r"^(?:reopen|bring back) (?:the |that )?(?:last |closed )?tab(?: i closed)?$",
       RouteKind.TOOL, "browser_tab", {"action": "reopen"}),
    _c(r"^(?:go )?back (?:a|one) page$|^(?:go to the )?previous page$",
       RouteKind.TOOL, "browser_tab", {"action": "back"}),
    _c(r"^(?:go )?forward (?:a|one) page$", RouteKind.TOOL, "browser_tab", {"action": "forward"}),
    _c(r"^(?:reload|refresh) (?:the |this )?(?:page|tab)$", RouteKind.TOOL, "browser_tab",
       {"action": "reload"}),
    _c(r"^(?:pause|stop) (?:the )?(?:music|song|track|playback)$", RouteKind.TOOL, "media_control",
       {"action": "pause"}),
    _c(r"^(?:resume|unpause) (?:the )?(?:music|song|track|playback)$|^play (?:the )?music$|"
       r"^(?:play|put on|stick on) some music$|^(?:put|stick) some music on$",
       RouteKind.TOOL, "media_control", {"action": "play"}),
    _c(r"^(?:next|skip)(?: this| the)? (?:song|track)$|^skip (?:this|it)$", RouteKind.TOOL,
       "media_control", {"action": "next"}),
    _c(r"^(?:previous|last) (?:song|track)$|^go back a (?:song|track)$", RouteKind.TOOL,
       "media_control", {"action": "previous"}),
    _c(r"^(?:turn on|enable|switch on|use|go) dark mode$|^switch (?:the |my )?(?:mac )?to dark mode$",
       RouteKind.TOOL, "set_appearance", {"mode": "dark"}),
    _c(r"^(?:turn off|disable|switch off) dark mode$|^(?:use|go) light mode$|"
       r"^switch (?:the |my )?(?:mac )?to light mode$", RouteKind.TOOL, "set_appearance", {"mode": "light"}),
    _c(r"^toggle dark mode$", RouteKind.TOOL, "set_appearance", {"mode": "toggle"}),
    _c(r"^lock (?:my |the |this )?(?:mac|screen|computer|laptop)$", RouteKind.TOOL, "lock_screen", {}),

    # -- applications ------------------------------------------------------
    # `safe=_is_concrete_app_name` on all three: a launch/close/focus target
    # that looks like a web destination, a bare reference or a thing inside
    # an app ("a new tab") is not a deterministic argument, whatever the
    # surrounding verb matched (V1.3 F2/F3).
    _c(rf"^(?:open|launch|start|run|fire up|bring up)\s+{_APP}{_TRAIL}",
       RouteKind.TOOL, "open_application", lambda m: {"name": m.group("app")},
       safe=_is_concrete_app_name),
    _c(rf"^(?:close|quit|exit|shut down|kill)\s+{_APP}{_TRAIL}",
       RouteKind.TOOL, "close_application", lambda m: {"name": m.group("app")},
       safe=_is_concrete_app_name),
    _c(rf"^(?:switch to|focus|activate|bring)\s+{_APP}(?:\s+to the front)?{_TRAIL}",
       RouteKind.TOOL, "activate_application", lambda m: {"name": m.group("app")},
       safe=_is_concrete_app_name),
    _c(r"^(?:what apps are|which apps are|what'?s) (?:currently )?(?:running|open)\??$",
       RouteKind.TOOL, "list_applications", {"running_only": True}),
    _c(r"^(?:what apps (?:do i have|are) installed|list (?:my )?(?:installed )?apps)\??$",
       RouteKind.TOOL, "list_applications", {}),

    # -- browser -----------------------------------------------------------
    _c(rf"^(?:go to|open|visit|take me to|navigate to)\s+(?:the )?(?:website\s+)?"
       rf"(?P<url>{_URL_SHAPE})[.!]?$",
       RouteKind.TOOL, "browse_to", lambda m: {"url": m.group("url")}),
    _c(r"^(?:search (?:the web |the internet |online )?for|google|look up on the web|"
       r"web search(?: for)?)\s+(?P<q>.+?)[.!?]?$",
       RouteKind.TOOL, "browse_to", lambda m: {"query": m.group("q")}, safe=_is_plain_web_search),
    _c(r"^(?:what page am i on|what'?s this page|what am i reading)\??$",
       RouteKind.TOOL, "get_current_page", {"include_text": False}),

    # -- clipboard ---------------------------------------------------------
    _c(r"^(?:what did i (?:just )?copy|what'?s (?:on|in) (?:my |the )?clipboard|"
       r"read (?:me )?(?:my |the )?clipboard|what'?s copied)\??[.!]?$",
       RouteKind.TOOL, "read_clipboard", {}),
    _c(r"^(?:copy|put)\s+(?P<text>.+?)\s+(?:to|on|in|into)\s+(?:my |the )?clipboard[.!]?$",
       RouteKind.TOOL, "write_clipboard", lambda m: {"text": m.group("text")}),

    # -- screen ------------------------------------------------------------
    _c(r"^(?:take|grab|capture)(?: a| the)? (?:screenshot|screen ?shot|screen ?grab|screen capture)[.!]?$",
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

    # -- system facts (anchored: a question about another device is chat) ----
    _c(r"^(?:how much (?:disk |storage |free )?(?:space|storage)(?: do i have| have i got| is "
       r"(?:left|free|available))?(?: left| free| available| remaining)?(?: on (?:my|this) "
       r"(?:mac|computer|laptop|disk|drive))?|(?:what'?s|check) (?:my |the )?(?:disk space|storage|"
       r"free space)(?: left)?|(?:storage|disk) (?:space )?(?:left|free|available)|disk space|"
       r"free space|space (?:left|remaining))\??$", RouteKind.TOOL, "get_storage", {}),
    _c(r"^(?:(?:what'?s|what is|check) (?:my |the )?battery(?: level| percentage| percent| status| health)?|"
       r"how'?s (?:my |the )?battery(?: doing| looking)?|how much battery (?:do i have|have i got|"
       r"is left|left)|battery(?: level| percentage| status)?|(?:what'?s|what is) (?:my |the )?"
       r"charge(?: level)?|how charged is (?:my|the) (?:mac|laptop|battery))\??$",
       RouteKind.TOOL, "get_battery", {}),
    _c(r"^(?:how much (?:ram|memory) (?:do i have|have i got|is (?:free|used|in use|available))|"
       r"(?:what'?s|check) (?:my |the )?(?:memory|ram)(?: usage| pressure)?|"
       r"memory (?:pressure|usage|used)|how much ram)\??$",
       RouteKind.TOOL, "get_memory", {}),
    _c(r"^(?:what (?:chip|processor|cpu) (?:does this|do i)(?: mac)? (?:have|use|run)|"
       r"which chip (?:is this|do i have)|what kind of mac (?:is this|do i have))\??$",
       RouteKind.TOOL, "get_system_info", {"field": "chip"}),
    _c(r"^(?:what (?:version of )?(?:macos|os|mac os)(?: version)? (?:am i (?:on|running)|is this|"
       r"do i have)|which macos(?: version)?(?: am i on| is this)?|macos version)\??$",
       RouteKind.TOOL, "get_system_info", {"field": "os"}),
    _c(r"^(?:how long has (?:this|my) mac been (?:on|up)|what'?s (?:my |the )?uptime|system uptime)\??$",
       RouteKind.TOOL, "get_system_info", {"field": "uptime"}),
    _c(r"^(?:system info(?:rmation)?|tell me about (?:this|my) mac|mac specs?|"
       r"what are my specs)\??$", RouteKind.TOOL, "get_system_info", {}),
    _c(r"^(?:(?:what'?s|check) (?:the |my )?cpu (?:usage|load)|cpu (?:usage|load)|"
       r"what'?s using (?:my |the )?cpu|how busy is (?:my |the )?(?:cpu|mac))\??$",
       RouteKind.TOOL, "get_cpu", {}),
    _c(r"^(?:am i (?:online|connected)(?: to the internet)?|what (?:wi-?fi|network) am i "
       r"(?:on|connected to)|network status|is (?:the |my )?(?:internet|wifi|wi-fi) "
       r"(?:working|up|on|connected))\??$", RouteKind.TOOL, "get_network", {}),
    _c(r"^(?:what'?s (?:using|eating) (?:my |the )?(?:memory|ram)|"
       r"(?:show|list) (?:me )?(?:the )?(?:top |heaviest )?processes)\??$",
       RouteKind.TOOL, "get_processes", {}),

    # -- volume ------------------------------------------------------------
    _c(r"^(?:mute|silence)(?: the)?(?: sound| volume| audio)?[.!]?$",
       RouteKind.TOOL, "set_volume", {"action": "mute"}),
    _c(r"^(?:unmute|restore sound)(?: the)?(?: sound| volume| audio)?[.!]?$",
       RouteKind.TOOL, "set_volume", {"action": "unmute"}),
    _c(r"^(?:set|turn|put) (?:the )?volume (?:to|at) (?P<level>\d{1,3})\s*(?:percent|per cent|%)?$",
       RouteKind.TOOL, "set_volume", lambda m: {"level": int(m.group("level")), "action": "set"}),
    _c(r"^(?:turn (?:the )?volume (up|down)|volume (up|down)|turn (up|down) the volume|"
       r"(?:crank|bump|pump|whack|turn) (?:the )?volume (up|down)(?: a bit| a little| a notch)?)$",
       RouteKind.CAPABILITY, "system", _volume_step),
    _c(r"^(?:what'?s (?:the )?volume|how loud)\??$", RouteKind.TOOL, "set_volume",
       {"action": "get"}),

    # -- diagnostics -------------------------------------------------------
    _c(r"^(?:why is (?:my |this )?mac (?:so )?(?:slow|sluggish|laggy|freezing|hot)|"
       r"what'?s wrong with (?:my |this )?mac|run (?:a )?diagnostics?|diagnose (?:my |this )?mac|"
       r"check (?:my |the )?system|is (?:something|anything) wrong with (?:my |this )?mac|"
       r"(?:run a |do a )?health check)\??$",
       RouteKind.CAPABILITY, "diagnostics", {}, 0.95, True),

    # -- email -------------------------------------------------------------
    _c(r"^(?:check (?:my )?(?:e-?mails?|mails?|inbox)|any new (?:e-?mails?|mail|messages)|"
       r"do i have (?:any )?(?:new )?(?:e-?mails?|mail)|read (?:my )?(?:e-?mails?|mail))\??$",
       RouteKind.CAPABILITY, "email", {"intent": "check"}, 0.96, True),

    # -- calendar ----------------------------------------------------------
    _c(r"^(?:what'?s on (?:today|my calendar|the calendar)(?: (?:today|to day|for today))?|"
       r"what'?s my schedule(?: today)?|what do i have (?:on )?today|(?:what'?s )?my agenda"
       r"(?: today| for today)?|any meetings today|what'?s happening today)\??$",
       RouteKind.TOOL, "read_calendar", {"range": "today"}, 0.96, True),
    _c(r"^(?:what'?s on tomorrow|tomorrow'?s schedule|what do i have (?:on )?tomorrow)\??$",
       RouteKind.TOOL, "read_calendar", {"range": "tomorrow"}, 0.96, True),
    _c(r"^(?:what'?s on this week|this week'?s schedule|what does my week look like)\??$",
       RouteKind.TOOL, "read_calendar", {"range": "week"}, 0.96, True),

    # -- files & workspace -------------------------------------------------
    _c(r"^(?:what'?s in my workspace|list (?:my )?(?:workspace|files)|show me my files)\??$",
       RouteKind.TOOL, "list_files", {}),
    _c(r"^(?:where is my workspace|workspace info(?:rmation)?)\??$",
       RouteKind.TOOL, "workspace_info", {}),
    _c(r"^(?:make|take|write|add) a note(?: that| saying| about)?\s+(?P<text>.+)$",
       RouteKind.TOOL, "create_note", lambda m: {"content": m.group("text")}),

    # -- memory ------------------------------------------------------------
    _c(r"^(?:what do you (?:remember|know) about me|what'?s in your memory|"
       r"what have you remembered)\??$", RouteKind.CAPABILITY, "memory", {"intent": "recall"}),
    _c(r"^(?:remember|note|keep in mind)(?: that)?\s+(?P<text>.+)$",
       RouteKind.CAPABILITY, "memory", lambda m: {"intent": "remember", "text": m.group("text")},
       safe=_is_a_fact_to_remember),
    _c(r"^forget(?: that| about)?\s*(?P<text>.*)$",
       RouteKind.CAPABILITY, "memory", lambda m: {"intent": "forget", "text": m.group("text")}),

    # -- research ----------------------------------------------------------
    _c(r"^(?:research|investigate|look into|dig into|find out about|compare)\s+(?P<q>.+)$",
       RouteKind.CAPABILITY, "research", lambda m: {"query": m.group("q")}, 0.95, True),
]

#: Verbs that start a second instruction ("… and *add* it to my basket").
_SECOND_VERB = (
    r"open|launch|start|run|close|quit|exit|go|visit|navigate|search|google|look|find|add|put|buy|"
    r"order|send|e-?mail|text|message|reply|forward|call|ring|remind|set|turn|switch|mute|unmute|"
    r"play|pause|stop|skip|take|save|copy|paste|move|delete|remove|rename|create|make|write|read|"
    r"check|tell|show|click|type|press|scroll|download|install|book|schedule|compare|research|"
    r"summari[sz]e|describe|lock|share|upload|print|log|sign|register|fill|select|choose|pick|"
    r"get|grab|bring|pull|pop|chuck|stick|drop|archive|translate|zip|empty|clear|connect|enable|"
    r"disable|update|restart|record|draft|attach|bookmark|refresh|reload|zoom|sort|tidy|"
    r"organi[sz]e|convert|resize|post|watch|listen|pin|star|mark|tick|see|explain|work out|"
    r"calculate|use|let|keep|note|then"
)
_SECOND_QUESTION = r"what'?s|whats|what|how|when|where|who|which|why|is|are|do|does|did|can|could|will|would|if"

#: A second clause: a joiner followed by a verb or a question word.
_COMPOUND = re.compile(
    rf"(?:,\s*|\s+)(?:and\s+then|and\s+also|then|also|and|after\s+that|afterwards|plus|before\s+that)"
    rf"\s+(?:please\s+)?(?:{_SECOND_VERB}|{_SECOND_QUESTION})\b"
    rf"|;\s*\w|,\s*(?:{_SECOND_VERB})\b",
    re.I,
)


def is_compound(text: str) -> bool:
    """Does *text* ask for more than one thing?"""
    return bool(_COMPOUND.search(text or ""))


class QuickCommands:
    """Deterministic matcher. Returns ``None`` when nothing matches cleanly."""

    def __init__(self, commands: list[QuickCommand] | None = None):
        self._commands = commands if commands is not None else COMMANDS

    def match(self, text: str) -> RouteDecision | None:
        t0 = time.perf_counter()
        cleaned = _normalise(text)
        if not cleaned:
            return None
        # One request, one clause: a second instruction needs a planner.
        compound = is_compound(cleaned)
        for index, candidate in enumerate(_variants(cleaned)):
            for command in self._commands:
                if compound and command.kind != RouteKind.CONTROL:
                    continue
                # Conversational controls ("send it", "yes") match only as
                # said: "please send it" with nothing waiting is a request
                # for the agent, not an approval.
                if index and command.kind == RouteKind.CONTROL:
                    continue
                found = command.pattern.search(candidate)
                if not found or not command.is_safe(found):
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

#: Ways of asking that don't change what's being asked.
_POLITE_PREFIX = re.compile(
    r"^(?:(?:hey |ok |okay |hi )?jarvis[,:]?\s+|please\s+|kindly\s+|just\s+|"
    r"(?:can|could|would|will) you(?: please| just| quickly)?\s+|any chance you could\s+|"
    r"i(?:'d| would) like you to\s+|i (?:want|need) you to\s+|go ahead and\s+)",
    re.I,
)
_POLITE_SUFFIX = re.compile(
    r"(?:[\s,]+(?:please|for me|real quick|quickly|thanks|thank you|right now|now|jarvis))+[\s.!?]*$",
    re.I,
)


def _normalise(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = _FILLERS.sub("", cleaned).strip().rstrip(",").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _variants(cleaned: str) -> list[str]:
    """The request as said, then with the politeness taken off — tried in
    that order, so a pattern that genuinely includes "can you" ("can you see
    my screen") still matches as written."""
    polite = cleaned
    while True:
        stripped = _POLITE_SUFFIX.sub("", _POLITE_PREFIX.sub("", polite)).strip()
        stripped = re.sub(r"[.!?]+$", "", stripped).strip()
        if stripped == polite:
            break
        polite = stripped
    return [cleaned] if polite == cleaned or not polite else [cleaned, polite]
