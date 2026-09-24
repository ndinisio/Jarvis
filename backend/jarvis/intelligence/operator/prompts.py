"""What the operator is told, and the tools that are about the task itself.

The system prompt never changes within a run, and the task brief after it
never changes either — everything that does change (what's been done, the
checklist's state, hints) comes after them. That keeps the front of every
request identical to the last one, so a local model's prompt cache reuses it
instead of re-reading thousands of tokens each step.
"""

from __future__ import annotations

from ...models.base import ToolDef

SYSTEM_PROMPT = """You operate this Mac for the user, one step at a time, using the tools you're given.
You see the result of every action, and after anything on a web page you see the page as it now is.

How to work:
- Act, don't narrate: reply with tool calls. Calls in one reply run in order, so you may send several
  that don't need to see each other's results (fill a field, then submit).
- On a web page, act on an element by the [handle] shown next to it in the latest page listing. Never
  make up a handle. If what you need isn't listed, scroll_page, read_page_manifest with an offset, or
  wait_for_page.
- A pop-up in the way: press_page_key escape, or click its close button.
- Never type a password or card details. A sign-in, CAPTCHA or two-factor check is the user's:
  ask_user_to_take_over.
- Paying, ordering, sending, deleting and installing are confirmed with the user automatically when
  you call the tool. Just call it; never ask permission yourself.
- If something only the user can tell you is missing (who to email, which one they meant), call
  ask_user with one short question. If it can't be done, call give_up and say why.
{finishing}"""

FINISH_PROVEN = """
Finishing: the task below has a checklist. When an item is achieved, call mark_done with its number and
a short quote copied exactly from a result or page you were shown that proves it (like "Added to
Basket" or "Message sent"). When every item is done, call finish with one or two sentences for the
user. finish is refused while any item lacks proof — then carry on with what's left."""

FINISH_ANSWER = """
Finishing: when the job is done, or you have what the user asked for, call finish with the answer for
the user in one or two sentences — built only from what the results showed."""

BRIEF = """Task: {goal}
{said}{criteria}{where}{recipes}{tips}{situation}{background}"""

RECIPES = ("Recipes: the skill_ tools each do a whole routine in one call. Use one when it fits; "
           "it says how far it got, and you carry on from there.\n")

#: How much of the live situation and of what's known about the user a brief
#: carries — it is part of every request in the run.
_SITUATION_CHARS = 1800
_BACKGROUND_CHARS = 900

#: The operator's own tools: about the task, not the computer. Never in the
#: tool registry; handled by the loop itself.
FINISH = ToolDef(
    name="finish",
    description="The task is complete (or you have the answer): tell the user, and stop.",
    parameters={"type": "object", "properties": {
        "summary": {"type": "string",
                    "description": "what to tell the user, in one or two sentences"},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "proof for any checklist items not yet marked, in order: "
                                    "quotes copied exactly from what you were shown"},
    }, "required": ["summary"]},
)
MARK_DONE = ToolDef(
    name="mark_done",
    description="Mark one checklist item as achieved, with a quote that proves it.",
    parameters={"type": "object", "properties": {
        "item": {"type": "integer", "description": "the checklist item's number"},
        "evidence": {"type": "string",
                     "description": "a short quote copied exactly from a result or page you were shown"},
    }, "required": ["item", "evidence"]},
)
ASK_USER = ToolDef(
    name="ask_user",
    description="Ask the user one short question, when something only they know is missing.",
    parameters={"type": "object", "properties": {
        "question": {"type": "string"},
    }, "required": ["question"]},
)
GIVE_UP = ToolDef(
    name="give_up",
    description="Stop because the task can't be done; say why.",
    parameters={"type": "object", "properties": {
        "reason": {"type": "string"},
    }, "required": ["reason"]},
)

CONTROL_TOOLS = (FINISH, MARK_DONE, ASK_USER, GIVE_UP)
CONTROL_NAMES = frozenset(tool.name for tool in CONTROL_TOOLS)


def system_prompt(*, proven: bool) -> str:
    return SYSTEM_PROMPT.format(finishing=FINISH_PROVEN if proven else FINISH_ANSWER)


def brief(goal: str, checklist, *, objective=None, said: str = "", situation: str = "",
          background: str = "", tips: str = "", recipes: bool = False) -> str:
    """The task as the model sees it — fixed for the whole run."""
    said = " ".join((said or "").split())
    said_line = (f"The user said: \u201c{said[:300]}\u201d\n"
                 if said and said.lower() != goal.strip().lower() else "")
    criteria = ""
    if checklist.gated:
        criteria = "Done means (the checklist):\n" + "\n".join(
            f"{index}. {item.text}" for index, item in enumerate(checklist.items, 1)) + "\n"
    details = [f"- {item}" for item in list(getattr(objective, "targets", None) or [])[:4]
               + list(getattr(objective, "constraints", None) or [])[:4]
               if isinstance(item, str) and item.strip()]
    if details:
        criteria = "Details:\n" + "\n".join(details) + "\n" + criteria
    where = ""
    site = (getattr(objective, "site", "") or "").strip()
    app = (getattr(objective, "app", "") or "").strip()
    if site or app:
        where = "Where: " + ", ".join(part for part in (site, app) if part) + "\n"
    situation = _clip(situation, _SITUATION_CHARS)
    now = f"\nThe situation right now:\n{situation}\n" if situation else ""
    # The situation already carries the clock.
    known = _clip("\n".join(line for line in (background or "").splitlines()
                            if not line.startswith("Current date and time")), _BACKGROUND_CHARS)
    context = f"\nWhat you know about the user:\n{known}\n" if known else ""
    tips = _clip(tips, 700)
    return BRIEF.format(goal=goal.strip(), said=said_line, criteria=criteria, where=where,
                        recipes=RECIPES if recipes else "",
                        tips=f"Tips for this site or app:\n{tips}\n" if tips else "",
                        situation=now, background=context).strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
