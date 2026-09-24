"""Structured decisions.

Every judgement the reasoning model makes comes back as validated JSON, not
prose to be regex-scraped. A malformed decision is a *recoverable* event: the
loader repairs what it can and otherwise returns ``None`` so the caller can
fall back, rather than a parser silently misreading an instruction.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..models.base import extract_json


class Complexity:
    """How much machinery a request deserves."""

    TRIVIAL = "trivial"      # answer directly, no tools
    SIMPLE = "simple"        # one tool call, maybe two
    MULTI_STEP = "multi_step"  # worth planning


class Confidence:
    CONFIDENT = "confident"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    IMPOSSIBLE = "impossible"


class EntityRef(BaseModel):
    """A thing the user referred to, before resolution."""

    text: str = ""
    kind: str = "unknown"   # person | app | url | email | file | result | screen_element
    resolved: str | None = None
    confidence: float = 0.5


class Objective(BaseModel):
    """What the user actually wants — the replacement for a capability label."""

    goal: str = ""
    #: Free-form verb describing the objective ("read email", "navigate", …).
    #: Kept descriptive rather than a fixed enum so new work doesn't need a new label.
    kind: str = "general"
    targets: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    references: list[EntityRef] = Field(default_factory=list)
    needs_tools: bool = True
    complexity: str = Complexity.SIMPLE
    confidence: str = Confidence.PROBABLE
    #: True when this turn modifies or corrects the previous objective rather
    #: than starting a new one ("only the medical ones", "that's not right").
    refines_previous: bool = False
    is_correction: bool = False
    missing: list[str] = Field(default_factory=list)
    #: What must be true when the work is done, in checkable terms ("a pack
    #: of AA batteries is in the Amazon basket") — what the operator checks
    #: its own work against before it says it has finished (v3.0).
    success_criteria: list[str] = Field(default_factory=list)
    #: Where the work happens: "web" | "native" | "either" | "none".
    surface: str = ""
    #: The website or app the user meant, when they named or implied one.
    site: str = ""
    app: str = ""

    @field_validator("complexity")
    @classmethod
    def _valid_complexity(cls, value: str) -> str:
        allowed = {Complexity.TRIVIAL, Complexity.SIMPLE, Complexity.MULTI_STEP}
        return value if value in allowed else Complexity.SIMPLE

    @field_validator("confidence")
    @classmethod
    def _valid_confidence(cls, value: str) -> str:
        allowed = {Confidence.CONFIDENT, Confidence.PROBABLE, Confidence.AMBIGUOUS,
                   Confidence.IMPOSSIBLE}
        return value if value in allowed else Confidence.PROBABLE

    @property
    def needs_planning(self) -> bool:
        return self.complexity == Complexity.MULTI_STEP


class Triage(BaseModel):
    """Chat or action — the one semantic decision every non-deterministic turn
    makes, before any tool-shaped machinery runs (V1.3).

    Triage decides; it does not execute and does not reply — there is no
    ``reply`` field on purpose. ``objective`` is populated only for
    mode="action", and only ever used directly (skipping a separate
    Understanding call) when it is confident and complete; see
    ``triage.objective_sufficient``.
    """

    mode: Literal["chat", "action"] = "chat"
    confidence: float = 0.5
    action_evidence: list[str] = Field(default_factory=list)
    #: The request restated as one plain instruction ("open a new tab in
    #: Safari") — tried against the deterministic fast path, so colloquial
    #: phrasing of a simple command still gets the millisecond answer.
    normalized_command: str = ""
    objective: Objective | None = None
    requires_tools: bool = False
    reason: str = ""

    @field_validator("mode", mode="before")
    @classmethod
    def _accept_act(cls, value: Any) -> Any:
        return "action" if str(value).strip().lower() in {"act", "action"} else value

    @field_validator("objective", mode="before")
    @classmethod
    def _empty_objective_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, dict) and not value else value

    @model_validator(mode="after")
    def _evidence_gates_action(self) -> Triage:
        """No action without positive evidence.

        A domain word mentioned in passing ("I hate dealing with email") is
        not a request. The V1.3 benchmark is what surfaced this as the gate
        that actually matters, not a keyword or a capability guess.
        """
        if self.mode == "action" and not self.action_evidence:
            self.mode = "chat"
            self.objective = None
        return self


class PlanStep(BaseModel):
    intent: str
    tool_hint: str | None = None
    done: bool = False
    note: str = ""


class Plan(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)
    rationale: str = ""

    @property
    def pending(self) -> list[PlanStep]:
        return [step for step in self.steps if not step.done]

    def summary(self) -> str:
        return " → ".join(step.intent for step in self.steps)


class AgentDecision(BaseModel):
    """One turn of the execution loop."""

    action: Literal["tool_call", "clarify", "respond", "complete"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    question: str | None = None
    content: str | None = None
    reason: str = ""

    @field_validator("arguments", mode="before")
    @classmethod
    def _coerce_arguments(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = extract_json(value)
            if parsed is not None:
                return parsed
        return {}

    def validate_shape(self) -> str | None:
        """Return a problem description when required fields are missing."""
        if self.action == "tool_call" and not self.tool:
            return "tool_call without a tool name"
        if self.action == "clarify" and not (self.question or "").strip():
            return "clarify without a question"
        if self.action == "respond" and not (self.content or "").strip():
            return "respond without content"
        return None


class Verification(BaseModel):
    """Did the action actually achieve what was intended?"""

    verified: bool = True
    confidence: float = 0.5
    problem: str = ""
    evidence: str = ""
    #: True when the check itself couldn't run (no cheap way to verify).
    skipped: bool = False


class RecoveryPlan(BaseModel):
    strategy: Literal["retry", "alternative_tool", "modify_arguments", "ask_user", "report"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    question: str | None = None
    reason: str = ""


def load(model: type[BaseModel], data: Any) -> Any | None:
    """Validate *data* into *model*, tolerating the ways small models misbehave.

    Accepts a dict, a JSON string, or prose containing JSON. Returns ``None``
    rather than raising so callers can fall back to a deterministic path.
    """
    if data is None:
        return None
    if isinstance(data, model):
        return data
    if isinstance(data, str):
        data = extract_json(data)
    if not isinstance(data, dict):
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        # One repair pass: drop unknown keys, which is the usual failure.
        known = set(model.model_fields)
        pruned = {k: v for k, v in data.items() if k in known}
        try:
            return model.model_validate(pruned)
        except ValidationError:
            return None
