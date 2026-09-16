"""Mac troubleshooting.

Measure first, explain second. The capability collects evidence with the
diagnostics tool, then asks the model to interpret *those findings* — it is
never asked to guess why a Mac is slow. The answer is always structured as:

    OBSERVED            what was measured
    LIKELY CAUSE        inference drawn from it
    RECOMMENDED ACTION  what to do next (destructive steps stay behind confirmation)
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..models.registry import Slot
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.diagnostics")


class DiagnosticsCapability(Capability):
    name = "diagnostics"
    description = "Investigate problems with the Mac."
    long_running = True

    async def handle(self, request: Request) -> Response:
        task = request.task

        def step(message: str, progress: float | None = None, **meta):
            if task is not None:
                self.deps.tasks.step(task, message, progress, **meta)

        step("Collecting system measurements…", 0.15, phase="collect")
        result = await self.call_tool("run_diagnostics", {}, request.ctx)
        if not result.ok:
            return Response(text=result.summary, error=result.error)

        report = result.data or {}
        findings = report.get("findings", [])
        observed = [f for f in findings if f["severity"] in {"warning", "critical"}]
        healthy = not observed

        step("Interpreting the findings…", 0.7, phase="interpret")
        if healthy:
            text = (
                "OBSERVED\n"
                + "\n".join(f"• {f['observation']}" for f in findings[:5])
                + "\n\nNothing here explains a problem — the machine looks healthy."
            )
            spoken = "Nothing's wrong as far as I can measure. " + (
                findings[0]["observation"] if findings else ""
            )
            return Response(text=text, spoken=spoken, display=result.display, data=report)

        interpretation = await self._interpret(request, report, observed)
        spoken = observed[0]["observation"]
        if len(observed) > 1:
            spoken += f" There {'are' if len(observed) > 2 else 'is'} {len(observed) - 1} other " \
                      f"issue{'s' if len(observed) > 2 else ''} worth your attention."
        return Response(text=interpretation, spoken=spoken, display=result.display, data=report)

    async def _interpret(self, request: Request, report: dict, observed: list[dict]) -> str:
        measurements = "\n".join(
            f"- [{f['severity']}] {f['area']}: {f['observation']}" for f in report.get("findings", [])
        )
        raw = report.get("raw", {})
        extra = []
        for key in ("top_cpu", "top_memory"):
            rows = raw.get(key) or []
            if rows:
                extra.append(
                    f"{key}: " + ", ".join(f"{r['name']} {r['cpu']:.0f}%cpu/{r['memory']:.0f}%mem"
                                           for r in rows[:4])
                )
        prompt = (
            "These are real measurements from the user's Mac. Explain the problem.\n\n"
            f"Measurements:\n{measurements}\n" + ("\n".join(extra)) + "\n\n"
            "Reply in exactly this structure, with no other text:\n"
            "OBSERVED\n<the measured facts, one per line, no speculation>\n\n"
            "LIKELY CAUSE\n<your inference from those facts, one short paragraph>\n\n"
            "RECOMMENDED ACTION\n<numbered steps, safest first; mark anything destructive as "
            "'requires confirmation'>"
        )
        try:
            completion = await self.models.complete(
                Slot.GENERAL,
                [
                    ChatMessage("system", "You are a meticulous macOS support engineer. "
                                          "You never invent measurements."),
                    ChatMessage("user", prompt),
                ],
                max_tokens=600,
                temperature=0.2,
            )
            if completion.text.strip():
                return completion.text.strip()
        except Exception as exc:
            log.debug("diagnostic interpretation unavailable: %s", exc)
        # Deterministic fallback keeps the structure even with no model running.
        lines = ["OBSERVED"]
        lines += [f"• {f['observation']}" for f in report.get("findings", [])[:6]]
        lines += ["", "LIKELY CAUSE",
                  observed[0]["observation"] + " is the most likely explanation.", "",
                  "RECOMMENDED ACTION"]
        lines += _remedies(observed)
        return "\n".join(lines)


_REMEDY_BY_AREA = {
    "memory": ["Quit the applications using the most memory.",
               "Restart if memory pressure stays high after that."],
    "storage": ["Empty the Trash and clear the Downloads folder.",
                "Use Storage Settings to offload large unused files."],
    "cpu": ["Quit or restart the process at the top of the CPU list.",
            "Check Activity Monitor for anything unexpected."],
    "process": ["Quit the offending application and reopen it."],
    "battery": ["Keep it charged above 20% where possible.",
                "Have the battery serviced if the condition isn't Normal."],
    "network": ["Toggle Wi-Fi off and on.", "Restart the router if the problem persists."],
    "crashes": ["Update the crashing application.",
                "Check Console → Crash Reports for the repeated signature."],
    "thermal": ["Let the machine cool and check for blocked vents.",
                "Avoid sustained heavy load until temperatures drop."],
}


def _remedies(observed: list[dict]) -> list[str]:
    """One continuously numbered list, most relevant area first."""
    seen: set[str] = set()
    steps: list[str] = []
    for finding in observed:
        area = finding["area"]
        if area in seen:
            continue
        seen.add(area)
        steps.extend(_REMEDY_BY_AREA.get(area, [f"Investigate {area} further."]))
    if not steps:
        steps = ["Nothing specific to act on."]
    return [f"{index}. {step}" for index, step in enumerate(steps, start=1)]
