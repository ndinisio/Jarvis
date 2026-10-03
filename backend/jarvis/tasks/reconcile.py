"""What a stranded task's checkpoints do and don't tell you.

When JARVIS stops mid-task (a crash, a kill, a power cut) the step
checkpoints in memory (``MemoryStore.task_steps``) are all that is left of
how far it got. The conversation, the browser and the app state that run was
using are gone with the process, so nothing here *resumes* anything and
nothing replays an action: that could do a thing twice to a machine that has
already moved on. It reports, in plain terms, what the record shows — which
action last reported success, what came after, what the task's own checklist
had confirmed — and says plainly that whatever was in flight when it stopped
may or may not have taken effect. Deciding what to do next is the user's.

"Reported success" is the tool's own report, not an independent check; only
the checklist items (which the operator proves from evidence before it ticks
them) are called confirmed.
"""

from __future__ import annotations

import json
from typing import Any


def describe_interrupted(steps: list[dict[str, Any]]) -> str:
    """One short paragraph on how far a stranded task got, from its recorded
    *steps* (oldest first, as ``MemoryStore.task_steps`` returns them)."""
    if not steps:
        return "No steps had been recorded, so there is no telling how far it got."
    last = steps[-1]
    total = int(last["seq"])
    parts = [f"{total} step{'s' if total != 1 else ''} recorded"]

    succeeded = next((s for s in reversed(steps) if s["tool"] and s["ok"] == 1), None)
    if succeeded is not None:
        parts.append(f"the last action to report success was “{succeeded['summary']}”")
    else:
        parts.append("no action had reported success")

    if last is not succeeded:
        failed = bool(last["tool"]) and last["ok"] == 0
        parts.append(f"the last thing recorded was “{last['summary']}”" + (" (it failed)" if failed else ""))

    proven = _latest_proven(steps)
    if proven:
        parts.append("its checklist had confirmed: " + "; ".join(proven))

    return ("; ".join(parts) + ". Whatever was in flight when it stopped may or may not have "
            "taken effect — check before running it again.")


def _latest_proven(steps: list[dict[str, Any]]) -> list[str]:
    for step in reversed(steps):
        try:
            recorded = json.loads(step.get("args_redacted") or "{}")
        except (TypeError, ValueError):
            continue
        proven = recorded.get("proven") if isinstance(recorded, dict) else None
        if proven:
            return [str(item) for item in proven]
    return []
