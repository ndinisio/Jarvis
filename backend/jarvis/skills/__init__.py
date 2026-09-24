"""Skills: recipes for common errands, built in or learned (v3.0).

A skill runs an errand's usual steps without a model deciding each one —
grounded in what's on screen, through the ordinary tool registry — and hands
over to the operator the moment a step doesn't fit. See :mod:`.model` for
the format, :mod:`.runner` for how one runs, :mod:`.library` for how one is
chosen and :mod:`.learning` for how one is learned.
"""

from .library import SkillLibrary
from .model import Skill, SkillError
from .runner import SkillOutcome, SkillRunner

__all__ = ["Skill", "SkillError", "SkillLibrary", "SkillOutcome", "SkillRunner"]
