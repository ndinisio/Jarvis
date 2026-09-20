"""Result verification.

"Opened the BBC" is not the same as "the BBC is open". V1.2 checks important
actions actually achieved what was intended, and the check is proportional to
risk and cost: a cheap read gets a cheap assertion, a state change gets a real
observation, and things that cannot be verified cheaply are marked skipped
rather than assumed good.

Verification runs on evidence the tool already returned wherever possible, so
the common case costs nothing extra.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..core.logging import get_logger
from ..tools.base import ToolResult
from .schema import Objective, Verification

log = get_logger("jarvis.intelligence.verify")

#: HTTP-ish failure signals that a page body may carry even on a 200.
_NOT_FOUND = re.compile(
    r"\b(404|not found|page (?:can'?t|cannot|could not) be found|no longer available|"
    r"page unavailable|error 404|doesn'?t exist)\b", re.I)


class Verifier:
    """Checks that an action did what the objective wanted."""

    def __init__(self, deps):
        self._deps = deps

    async def verify(self, tool: str, arguments: dict, result: ToolResult,
                     objective: Objective, state) -> Verification:
        # A tool that reported failure needs no further proof.
        if not result.ok:
            return Verification(verified=False, confidence=0.95,
                                problem=result.summary or "the tool reported a failure",
                                evidence=result.error or "")

        spec = self._spec(tool)
        category = spec.category if spec else ""

        if category == "browser" or tool in {"browse_to", "open_url", "get_current_page"}:
            return self._verify_navigation(arguments, result, objective, state)
        if category == "files":
            return self._verify_file(tool, arguments, result)
        if category == "macos" and tool in {"open_application", "activate_application"}:
            return await self._verify_application(arguments, result)
        # Vision output, specifically — dispatching on the category alone would
        # send a click through a check written for a description.
        if category == "screen" and isinstance(result.data, dict) and "answer" in result.data:
            return self._verify_screen(result)

        # Reads are self-evidencing: the data came back or it didn't.
        if spec is not None and not spec.changes_state:
            has_data = result.data not in (None, {}, [], "")
            return Verification(verified=has_data, confidence=0.6 if has_data else 0.5,
                                problem="" if has_data else "the tool returned nothing",
                                evidence="result contained data" if has_data else "",
                                skipped=False)
        return Verification(verified=True, confidence=0.4, skipped=True,
                            evidence="no cheap way to verify this action")

    # -- per-category checks ----------------------------------------------
    def _verify_navigation(self, arguments: dict, result: ToolResult,
                           objective: Objective, state) -> Verification:
        data = result.data if isinstance(result.data, dict) else {}
        url = str(data.get("url") or arguments.get("url") or "")
        title = str(data.get("title") or "")
        text = str(data.get("text") or "")

        if not url:
            return Verification(verified=True, confidence=0.3, skipped=True,
                                evidence="no page information came back")

        # Did the page itself say it doesn't exist?
        body = f"{title}\n{text[:1500]}"
        if _NOT_FOUND.search(body):
            return Verification(verified=False, confidence=0.9,
                                problem=f"{_domain(url)} returned a not-found page",
                                evidence=(title or text[:120]))

        # Does the destination resemble what was asked for?
        wanted = self._intended_target(arguments, objective, state)
        if wanted:
            haystack = f"{_domain(url)} {title}".lower()
            if not any(token in haystack for token in _tokens(wanted)):
                return Verification(
                    verified=False, confidence=0.6,
                    problem=f"landed on {_domain(url)}, which doesn't look like “{wanted}”",
                    evidence=f"url={url} title={title[:80]}")
        return Verification(verified=True, confidence=0.75,
                            evidence=f"on {_domain(url)}" + (f" — {title[:60]}" if title else ""))

    @staticmethod
    def _verify_file(tool: str, arguments: dict, result: ToolResult) -> Verification:
        from pathlib import Path

        data = result.data if isinstance(result.data, dict) else {}
        path = str(data.get("path") or arguments.get("path") or "")
        if not path:
            return Verification(verified=True, confidence=0.4, skipped=True)
        exists = Path(path).expanduser().exists()
        if tool == "delete_file":
            return Verification(verified=not exists, confidence=0.9,
                                problem="" if not exists else "the file is still there",
                                evidence=f"{path} {'removed' if not exists else 'present'}")
        return Verification(verified=exists, confidence=0.9,
                            problem="" if exists else "the file isn't where it should be",
                            evidence=f"{path} {'exists' if exists else 'missing'}")

    async def _verify_application(self, arguments: dict, result: ToolResult) -> Verification:
        data = result.data if isinstance(result.data, dict) else {}
        name = str(data.get("application") or arguments.get("name") or "")
        if not name:
            return Verification(verified=True, confidence=0.3, skipped=True)
        try:
            running = await self._deps.controller.is_app_running(name)
        except Exception as exc:  # pragma: no cover - platform dependent
            log.debug("could not check whether %s is running: %s", name, exc)
            return Verification(verified=True, confidence=0.3, skipped=True,
                                evidence="could not query application state")
        return Verification(verified=running, confidence=0.85,
                            problem="" if running else f"{name} doesn't appear to be running",
                            evidence=f"{name} {'is running' if running else 'is not running'}")

    @staticmethod
    def _verify_screen(result: ToolResult) -> Verification:
        data = result.data if isinstance(result.data, dict) else {}
        answer = str(data.get("answer") or "")
        if not answer:
            return Verification(verified=False, confidence=0.7,
                                problem="the screen was captured but not described")
        # Vision models hedge when they can't see; treat that as unverified
        # rather than pretending the description is usable.
        if re.search(r"\b(can'?t (see|tell)|unable to (see|determine)|no (visible|clear))\b",
                     answer, re.I):
            return Verification(verified=False, confidence=0.6,
                                problem="the screen description was inconclusive",
                                evidence=answer[:120])
        return Verification(verified=True, confidence=0.7, evidence=answer[:120])

    # -- helpers -----------------------------------------------------------
    def _spec(self, tool: str):
        registered = self._deps.registry.get(tool)
        return registered.spec if registered else None

    @staticmethod
    def _intended_target(arguments: dict, objective: Objective, state) -> str:
        """What the user actually named, for comparison against where we landed."""
        for candidate in (arguments.get("query"), *(objective.targets or []),
                          getattr(state.browser, "intended", "")):
            if candidate and not str(candidate).startswith("http"):
                return str(candidate)
        url = str(arguments.get("url") or "")
        return _domain(url) if url else ""


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url
    except ValueError:
        return url


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower())}
