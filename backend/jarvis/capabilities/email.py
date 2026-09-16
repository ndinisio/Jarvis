"""Email.

Reading, triage and drafting. Sending is a separate, explicitly confirmed step —
a model deciding that a message "should" be sent is not sufficient authority,
and the send tool is HIGH risk precisely so it cannot happen silently.
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..models.registry import Slot
from ..tools.email.mail_app import person_name
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.email")


class EmailCapability(Capability):
    name = "email"
    description = "Read, search, summarise and draft email."
    long_running = True

    async def handle(self, request: Request) -> Response:
        intent = request.args.get("intent") or self._intent(request.text)
        if intent == "search":
            return await self._search(request)
        if intent == "draft":
            return await self._draft(request)
        return await self._check(request)

    # -- intents -----------------------------------------------------------
    @staticmethod
    def _intent(text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ("draft", "reply", "write to", "compose", "respond")):
            return "draft"
        if any(word in lowered for word in ("search", "find", "from ", "about ", "look for")):
            return "search"
        return "check"

    async def _check(self, request: Request) -> Response:
        task = request.task
        if task is not None:
            self.deps.tasks.step(task, "Opening Mail…", 0.2, phase="mail")
        result = await self.call_tool("check_email", {"limit": 8}, request.ctx)
        if not result.ok:
            return Response(text=result.summary, error=result.error, display=result.display)

        data = result.data or {}
        messages = data.get("messages", [])
        if not messages:
            return Response(text="No new mail.", display=result.display)

        if task is not None:
            self.deps.tasks.step(task, "Summarising the important ones…", 0.7, phase="summarise")
        detail = await self._summarise(messages, request)
        spoken = result.summary
        return Response(text=detail, spoken=spoken, display=result.display, data=data)

    async def _search(self, request: Request) -> Response:
        query = await self._extract_query(request.text)
        result = await self.call_tool("search_email", {"query": query}, request.ctx)
        return Response(text=result.summary, display=result.display, error=result.error,
                        data=result.data)

    async def _draft(self, request: Request) -> Response:
        plan = await self._plan_draft(request)
        if not plan:
            return Response(
                text="I'll need the recipient and roughly what you'd like to say."
            )
        result = await self.call_tool("draft_email", plan, request.ctx)
        if not result.ok:
            return Response(text=result.summary, error=result.error)
        body = plan.get("body", "")
        text = (f"Draft to {', '.join(plan.get('to', []))}\n"
                f"Subject: {plan.get('subject','')}\n\n{body}\n\n"
                "Nothing has been sent. Say “send it” and confirm, and I will.")
        return Response(text=text, spoken=result.summary, display=result.display)

    # -- helpers -----------------------------------------------------------
    async def _summarise(self, messages: list[dict], request: Request) -> str:
        lines = []
        for message in messages[:8]:
            flag = "!" if message.get("urgency") == "high" else " "
            lines.append(
                f"{flag} {person_name(message.get('sender',''))} — {message.get('subject','')}\n"
                f"   {message.get('preview','')[:160]}"
            )
        listing = "\n".join(lines)
        try:
            completion = await self.models.complete(
                Slot.GENERAL,
                [
                    ChatMessage("system", "You triage a Mac user's inbox. Be terse and factual."),
                    ChatMessage(
                        "user",
                        "Summarise this inbox. One line per message: who, what it wants, and "
                        "whether it needs action today. Put anything urgent first.\n\n" + listing,
                    ),
                ],
                max_tokens=420,
                temperature=0.2,
            )
            if completion.text.strip():
                return completion.text.strip()
        except Exception as exc:
            log.debug("inbox summary unavailable: %s", exc)
        return listing

    async def _extract_query(self, text: str) -> str:
        import re

        match = re.search(r"(?:about|regarding|from|for|mentioning)\s+(.+)$", text, re.I)
        if match:
            return match.group(1).strip(" ?.")
        return re.sub(r"^(?:search|find|look for)\s+(?:my |the )?(?:e-?mails?|mail|inbox)\s*",
                      "", text, flags=re.I).strip(" ?.")

    async def _plan_draft(self, request: Request) -> dict | None:
        prompt = (
            "Extract an email to draft from the user's request. "
            "If the recipient is unclear, use an empty list.\n\n"
            f'Request: "{request.text}"\n\n'
            'Reply with JSON only: {"to": ["…"], "subject": "…", "body": "…"}'
        )
        try:
            data = await self.models.complete_json(
                Slot.GENERAL,
                [ChatMessage("system", "You draft email. JSON only."), ChatMessage("user", prompt)],
                max_tokens=500,
            )
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("to") or not data.get("body"):
            return None
        return {
            "to": data.get("to"),
            "subject": data.get("subject") or "(no subject)",
            "body": data.get("body"),
        }
