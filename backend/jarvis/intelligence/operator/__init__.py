"""The operator (v3.0): one loop for everything JARVIS does on the computer.

The per-turn agent runs it in the foreground for short actions and
questions; the automation capability runs it as a background ``Task`` for
multi-step errands. See :mod:`.loop` for how it works and why.
"""

from .checklist import Checklist
from .loop import Budget, Operator, OperatorResult, Status, StepReport
from .observation import OBSERVE_AFTER, ObservationLog

__all__ = [
    "OBSERVE_AFTER",
    "Budget",
    "Checklist",
    "ObservationLog",
    "Operator",
    "OperatorResult",
    "Status",
    "StepReport",
]
