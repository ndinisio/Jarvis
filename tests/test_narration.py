"""ActionNarrator: the two speech triggers and their shared throttle."""

from __future__ import annotations

from jarvis.core.narration import ActionNarrator


class _FakeVoice:
    def __init__(self):
        self.spoken: list[str] = []

    def enqueue(self, text: str) -> None:
        self.spoken.append(text)


def _narrator(app, *, min_gap_s=4.0, threshold_s=5.0):
    app.config_store.update({
        "voice": {"enabled": True},
        "automation": {"narration_min_gap_s": min_gap_s, "narration_action_threshold_s": threshold_s},
    })
    voice = _FakeVoice()
    app.deps.voice = voice
    return ActionNarrator(app.deps), voice


def test_phase_boundaries_are_always_spoken(app):
    narrator, voice = _narrator(app)
    assert narrator.phase("Opening Safari and searching…") is True
    assert voice.spoken == ["Opening Safari and searching…"]


def test_a_fast_step_stays_silent(app):
    narrator, voice = _narrator(app)
    assert narrator.maybe_narrate("Read the manifest.", expected_ms=200, elapsed_ms=50) is False
    assert voice.spoken == []


def test_a_slow_step_is_narrated(app):
    narrator, voice = _narrator(app)
    assert narrator.maybe_narrate("Waiting for the page to load.", expected_ms=8000) is True
    assert voice.spoken


def test_a_step_that_actually_ran_long_is_narrated_even_with_a_low_expected_ms(app):
    narrator, voice = _narrator(app)
    assert narrator.maybe_narrate("That took a while.", expected_ms=200, elapsed_ms=6000) is True
    assert voice.spoken


def test_the_shared_throttle_prevents_two_lines_close_together(app):
    narrator, voice = _narrator(app, min_gap_s=10.0)
    assert narrator.phase("Starting.") is True
    # A slow step immediately after a phase announcement must not talk over it.
    assert narrator.maybe_narrate("Slow step.", expected_ms=9000) is False
    assert voice.spoken == ["Starting."]


def test_no_voice_manager_means_narration_is_a_silent_no_op(app):
    app.config_store.update({"voice": {"enabled": True}})
    app.deps.voice = None
    narrator = ActionNarrator(app.deps)
    assert narrator.phase("Starting.") is False


def test_voice_disabled_in_config_means_no_narration_even_with_a_voice_manager(app):
    narrator, voice = _narrator(app)
    app.config_store.update({"voice": {"enabled": False}})
    assert narrator.phase("Starting.") is False
    assert voice.spoken == []


def test_an_empty_message_narrates_nothing(app):
    narrator, voice = _narrator(app)
    assert narrator.phase("") is False
    assert voice.spoken == []
