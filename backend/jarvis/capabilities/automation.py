"""Errands: multi-step work on apps and websites, as a background task.

    (confirm, if the user wants that) → operator loop → report

The work itself is the operator's (:mod:`jarvis.intelligence.operator`) —
the same loop a short foreground action runs on. What this capability adds
is everything that makes it an errand rather than a turn:

* **A real ``Task``.** It is reached through the orchestrator's
  backgrounding machinery (``_handoff_to_automation`` →
  ``_handle_capability`` → ``_run_in_background``), so the user can keep
  talking, "stop" reaches it through the task's ``cancel_event``, and every
  action lands in the task's step trail with the checklist's live state.
* **An errand-sized budget** (``automation.max_steps``/``max_wall_s``/
  ``max_model_calls``) and **a checklist it must prove** before it may say
  it is done — the interpreter's success criteria, or the goal itself.
* **A task-scoped permission grant** (``security/permissions.py:
  grant_task``): routine steps run without a prompt each, but nothing the
  consequence classifier marks consequential — spending money, sending,
  deleting, installing — is ever covered by it; each of those asks.
* **Narration** of slow steps and proven items, when voice is on, and an
  honest report at the end: what was done, and what wasn't.
"""

from __future__ import annotations

import re

from ..core.errors import ConfirmationDeclined
from ..core.logging import get_logger
from ..core.narration import ActionNarrator
from ..intelligence.catalog import ToolCatalog
from ..intelligence.observability import Trace
from ..intelligence.operator import Budget, Checklist, Operator, OperatorResult, Status, StepReport
from ..intelligence.schema import Complexity, Objective
from ..models.base import ChatMessage
from ..models.registry import Slot
from ..security.permissions import RiskLevel
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.automation")

REPORT_PROMPT = """Tell the user how the errand went, in one or two sentences unless they asked \
for detail. Say what was actually done, and say plainly anything that wasn't.

They asked: {goal}
{checklist}
What happened (most recent last):
{findings}
{ending}
Answer directly. Do not describe your process or mention tool names."""

#: The errand toolkit, always offered: operating web pages and apps, reading
#: the web and the screen, files. Anything else an errand needs (mail, notes,
#: the calendar…) joins it from the same shortlist a foreground turn uses.
WEB_TOOLS: tuple[str, ...] = (
    "browse_to", "get_current_page", "list_browser_tabs", "read_page_manifest",
    "click_page_element", "fill_page_field", "submit_page_form", "press_page_key",
    "scroll_page", "page_go_back", "wait_for_page", "ask_user_to_take_over",
)
NATIVE_TOOLS: tuple[str, ...] = (
    "open_application", "activate_application", "get_frontmost_app", "list_windows",
    "click_element", "type_text", "press_key", "wait_for_element", "scroll",
)
READ_TOOLS: tuple[str, ...] = (
    "search_web", "fetch_page", "fetch_pages", "capture_screen", "analyse_screen",
)
SHELL_TOOLS: tuple[str, ...] = ("run_shell_command",)
DOWNLOAD_TOOLS: tuple[str, ...] = ("download_file",)
INSTALL_TOOLS: tuple[str, ...] = ("run_installer",)

ALL_AUTOMATION_TOOLS: tuple[str, ...] = (
    WEB_TOOLS + NATIVE_TOOLS + READ_TOOLS + SHELL_TOOLS + DOWNLOAD_TOOLS + INSTALL_TOOLS
)

#: What an errand says it involves → the tools that involves, beyond the
#: toolkit. Word starts, so "remind" covers "reminder" and "remind me".
_ERRAND_EXTRAS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("email", "e-mail", "mail", "inbox", "reply"),
     ("search_email", "read_email", "draft_email", "send_email", "search_contacts")),
    (("message", "text", "imessage", "sms"),
     ("search_messages", "read_messages", "send_message", "search_contacts")),
    (("calendar", "meeting", "event", "appointment", "diary"),
     ("read_calendar", "search_calendar", "create_calendar_event")),
    (("remind", "to-do", "todo"),
     ("list_reminders", "search_reminders", "create_reminder", "complete_reminder")),
    (("note",), ("create_note",)),
    (("file", "folder", "document", "desktop", "spreadsheet", "pdf", "save"),
     ("list_files", "search_files", "read_file", "write_file", "move_file")),
    (("delete", "remove", "trash", "get rid of"), ("delete_file",)),
    (("clipboard", "copy", "paste"), ("read_clipboard", "write_clipboard")),
    (("contact", "phone number", "address"), ("search_contacts",)),
    (("music", "song", "playlist", "album", "volume"), ("media_control", "set_volume")),
    (("dark mode", "light mode", "appearance"), ("set_appearance",)),
    (("notif",), ("send_notification",)),
)
#: Extra tools taken from the objective's shortlist, for anything else.
_SHORTLIST_EXTRAS = 4


class AutomationCapability(Capability):
    name = "automation"
    description = "Operate apps and websites through as many real steps as an errand takes."
    long_running = True

    def __init__(self, deps):
        super().__init__(deps)
        self._narrator = ActionNarrator(deps)

    async def handle(self, request: Request) -> Response:
        conf = self.deps.config.automation
        objective = request.args.get("objective")
        if not isinstance(objective, Objective):
            objective = Objective(goal=request.text, complexity=Complexity.MULTI_STEP)
        goal = (objective.goal or request.text).strip()

        request.ctx.raise_if_cancelled()
        if self.deps.config.security.autonomy == "confirm_start":
            try:
                await self._confirm_start(goal, objective)
            except ConfirmationDeclined as exc:
                # Unlike a tool call's decline (converted to a ToolResult by
                # the registry), this require() is called directly, inside a
                # background Task — an uncaught raise would be swallowed as a
                # bare failure, leaving silence after "I'm on it".
                return Response(text=exc.user_message, spoken=exc.user_message)

        task_id = request.ctx.task_id
        if task_id:
            self.deps.permissions.grant_task(task_id)
        intelligence = self.deps.config.intelligence
        trace = Trace(self.deps.bus if intelligence.trace else None, self.deps.telemetry,
                      verbose=self.deps.config.ui.developer_mode, task_id=task_id)
        progress = _Progress(self, request)
        try:
            result = await Operator(self.deps, slot=Slot.OPERATOR, trace=trace).run(
                goal, request.ctx, tools=self.tools_for(objective), objective=objective,
                budget=Budget(steps=conf.max_steps, wall_s=conf.max_wall_s,
                              model_calls=conf.max_model_calls),
                background=True, said=request.text,
                situation=str(request.args.get("situation") or ""), context=request.context,
                after=progress.step,
            )
        finally:
            if task_id:
                self.deps.permissions.revoke_task(task_id)
                if self.deps.browsers is not None:
                    self.deps.browsers.release(task_id)

        if result.status == Status.ASKED:
            return Response(text=result.question, clarification=result.question,
                            display=_display(goal, result), data=_data(goal, result))
        report = await self._report(goal, result)
        return Response(text=report, display=_display(goal, result), data=_data(goal, result))

    def tools_for(self, objective: Objective) -> list[str]:
        """The errand toolkit plus whatever this objective specifically needs.

        Every tool offered is paid for in every request (its schema is part of
        the prompt), so the rest of the registry isn't offered wholesale: an
        errand that mentions email gets the mail tools, one that mentions a
        reminder gets those, and the objective's own best matches join them.
        """
        wanted = list(ALL_AUTOMATION_TOOLS)
        about = " ".join([objective.goal, objective.kind, objective.app, objective.site,
                          *objective.targets, *objective.constraints]).lower()
        for words, tools in _ERRAND_EXTRAS:
            if any(re.search(rf"(?<![a-z]){re.escape(word)}", about) for word in words):
                wanted.extend(tools)
        wanted.extend(card.name for card in
                      ToolCatalog(self.registry).shortlist(objective, None, limit=_SHORTLIST_EXTRAS))
        return [name for name in dict.fromkeys(wanted) if self.registry.get(name) is not None]

    # -- the report -------------------------------------------------------------
    async def _report(self, goal: str, result: OperatorResult) -> str:
        """What happened, honestly: done, not done, and why it stopped."""
        if result.status == Status.FINISHED and result.answer:
            return result.answer
        if result.status == Status.DECLINED:
            before = f" Before that: {_join(result.done)}." if result.done else ""
            return (result.reason or "Understood — I've left it alone.") + before
        if not result.findings:
            if result.status == Status.UNAVAILABLE:
                return ("The local AI service isn't available, sir, so I couldn't start on that. "
                        "Start Ollama and ask me again.")
            if result.status == Status.GAVE_UP:
                return f"I couldn't do that, sir — {result.reason.rstrip('.')}."
            return "I wasn't able to make progress on that, sir."
        ending = {
            Status.GAVE_UP: f"It couldn't be finished: {result.reason}",
            Status.BUDGET: f"Work stopped before the end: {result.reason}",
            Status.STALLED: "Work stopped before the end.",
            Status.UNAVAILABLE: "The model stopped responding before the end.",
        }.get(result.status, "")
        checklist = ""
        if result.checklist:
            checklist = "\nChecklist:\n" + "\n".join(
                f"- [{'done' if item['done'] else 'not done'}] {item['text']}"
                for item in result.checklist) + "\n"
        prompt = REPORT_PROMPT.format(
            goal=goal, checklist=checklist,
            findings="\n".join(f"- {f}" for f in result.findings[-12:]),
            ending=f"\nHow it ended: {ending}\n" if ending else "")
        try:
            completion = await self.models.complete(
                Slot.GENERAL,
                [ChatMessage("system", "You report back plainly on completed work."),
                 ChatMessage("user", prompt)],
                max_tokens=300, temperature=0.3,
            )
            if completion.text.strip():
                return completion.text.strip()
        except Exception as exc:
            log.debug("automation report unavailable: %s", exc)
        return _plain_report(result, ending)

    # -- confirmation ------------------------------------------------------------
    async def _confirm_start(self, goal: str, objective: Objective) -> None:
        items = [item.text for item in Checklist.for_objective(objective, goal, background=True).items]
        plan = "; ".join(items)
        summary = (
            f"I'll {goal.rstrip('.')}. "
            + (f"Done means: {plan}. " if plan and plan != goal else "")
            + "I'll ask again before anything that spends money, deletes something, sends "
              "something, or runs an installer."
        )
        await self.deps.permissions.require(
            action="automation:start", risk=RiskLevel.MEDIUM, summary=summary,
            details={"goal": goal, "checklist": items},
        )


class _Progress:
    """Each action into the task's step trail — and, when it's slow or it
    proves a checklist item, into speech."""

    def __init__(self, capability: AutomationCapability, request: Request):
        self._deps = capability.deps
        self._narrator = capability._narrator
        self._request = request
        self._proven: set[str] = set()

    def step(self, report: StepReport) -> None:
        task = self._request.task
        if task is not None:
            self._deps.tasks.step(task, report.message, tool=report.tool, ok=report.ok,
                                  checklist=report.checklist)
        else:
            self._request.ctx.report(report.message, tool=report.tool)
        newly = [item["text"] for item in report.checklist
                 if item.get("done") and item["text"] not in self._proven]
        self._proven.update(newly)
        if newly:
            self._narrator.phase(f"{newly[-1]} — done.")
        else:
            self._narrator.maybe_narrate(report.message, expected_ms=report.expected_ms,
                                         elapsed_ms=report.elapsed_ms)


def _display(goal: str, result: OperatorResult) -> dict:
    return {"kind": "automation", "title": goal[:90], "status": result.status,
            "checklist": result.checklist, "findings": result.findings[-16:]}


def _data(goal: str, result: OperatorResult) -> dict:
    return {"goal": goal, "status": result.status, "checklist": result.checklist,
            "findings": result.findings, "steps": result.steps,
            "model_calls": result.model_calls}


def _plain_report(result: OperatorResult, ending: str) -> str:
    """The report without a model to phrase it."""
    parts = []
    if result.done:
        parts.append(f"Done: {_join(result.done)}.")
    if result.not_done:
        parts.append(f"Not done: {_join(result.not_done)}.")
    if not parts:
        parts.append("; ".join(f.split(" → ", 1)[-1] for f in result.findings[-3:]) + ".")
    if ending:
        parts.append(ending.rstrip(".") + ".")
    return " ".join(parts)


def _join(items: list[str]) -> str:
    items = [item.rstrip(".") for item in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"
