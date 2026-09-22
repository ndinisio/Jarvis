"""HomeKit, via a named Shortcut.

Home.app has no AppleScript dictionary, and HomeKit's own device-pairing
protocol (HAP) isn't scriptable from outside Apple's own apps — confirmed
by exploration, not assumed: there is no generic "discover and control any
accessory" path available on macOS at all, free or otherwise. The one real
bridge is the ``shortcuts`` command-line tool (built into macOS since
Monterey, free), which can run a named Shortcut the user has already
authored in the Shortcuts app, and Shortcuts itself *can* contain HomeKit
actions (turn an accessory on/off, run a scene, and so on).

That makes this a fundamentally thinner integration than every other tool
in this package: JARVIS can see a Shortcut's *name*, never what it
actually does. A Shortcut named "Turn Off Lights" could just as easily
delete files or send a message — nothing here can tell the difference
computationally. That is the whole reason ``run_home_shortcut`` is HIGH
risk with ``always_confirm_individually=True`` unconditionally, regardless
of what its name suggests: the confirmation prompt, which names the exact
Shortcut about to run, is the one real safety net available here, not a
judgement this file can make on the user's behalf.

Setup is on the user, stated plainly rather than solved generically:
there's no free way to discover or control a HomeKit accessory directly,
so each accessory or scene JARVIS should reach needs its own named
Shortcut authored ahead of time in the Shortcuts app.
"""

from __future__ import annotations

from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec

_SHORTCUTS_BIN = "/usr/bin/shortcuts"

#: Generous enough that a realistic Shortcuts library is never actually
#: truncated, while still bounding how much a pathological one could bloat
#: the prompt a capability builds from this list.
_MAX_LISTED = 300


class ListHomeShortcutsTool(Tool):
    spec = ToolSpec(
        name="list_home_shortcuts",
        description="List the Shortcuts available to run — how JARVIS reaches HomeKit "
                    "accessories and scenes, each set up ahead of time as a named Shortcut",
        risk=RiskLevel.LOW,
        category="homekit",
        requires_macos=True,
        expected_ms=1500,
        examples=["what shortcuts do I have?", "what can you control at home?"],
        returns="the names of Shortcuts available to run",
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._deps.controller.run([_SHORTCUTS_BIN, "list"], timeout=15.0)
        if not result.ok:
            return ToolResult.failure("I couldn't list your Shortcuts.", detail=result.output[:500])
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()][:_MAX_LISTED]
        if not names:
            return ToolResult(data={"shortcuts": []}, summary="No Shortcuts are set up.")
        return ToolResult(
            data={"shortcuts": names},
            summary=f"{len(names)} Shortcuts available.",
            display={"kind": "list", "title": "Shortcuts", "items": names},
        )


class RunHomeShortcutTool(Tool):
    spec = ToolSpec(
        name="run_home_shortcut",
        description="Run a named Shortcut — always confirms first, individually, every time, "
                    "since JARVIS can't see what a Shortcut actually does",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        risk=RiskLevel.HIGH,
        category="homekit",
        requires_macos=True,
        mutates=True,
        retryable=False,
        always_confirm_individually=True,
        confirmation_template='Run the "{name}" Shortcut? I can\'t see what it actually does.',
        expected_ms=5000,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip()
        if not name:
            return ToolResult.failure("I need the name of a Shortcut to run.")
        result = await self._deps.controller.run([_SHORTCUTS_BIN, "run", name], timeout=30.0)
        if not result.ok:
            return ToolResult.failure(f"The “{name}” Shortcut didn't run.",
                                      detail=result.output[:500])
        return ToolResult(data={"name": name}, summary=f"Ran “{name}”.")


def homekit_tools(deps) -> list[Tool]:
    return [ListHomeShortcutsTool(deps), RunHomeShortcutTool(deps)]
