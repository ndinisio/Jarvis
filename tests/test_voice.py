"""Voice: speech shaping, engine selection and graceful degradation."""

from __future__ import annotations

import asyncio

from jarvis.core.personality import Personality, sentences, speakable
from jarvis.voice.manager import VoiceManager, VoiceState
from jarvis.voice.stt import build_stt
from jarvis.voice.tts import BrowserTTS, NullTTS, build_tts
from jarvis.voice.wakeword import build_wake_detector


def test_speakable_strips_markup_and_urls():
    spoken = speakable("**Result:** see [the docs](https://example.com/guide) for more.")
    assert "**" not in spoken and "https://" not in spoken
    assert "the docs" in spoken  # link text is spoken, the URL is not

    bare = speakable("The answer is at https://example.com/some/long/path?q=1")
    assert "https://" not in bare and "example.com" in bare


def test_speakable_truncates_at_a_sentence_boundary():
    text = " ".join(f"Sentence number {i} about something." for i in range(40))
    spoken = speakable(text, max_chars=200)
    assert len(spoken) <= 210
    assert spoken.rstrip().endswith((".", "…"))


def test_sentence_splitting_for_tts():
    assert sentences("First. Second! Third?") == ["First.", "Second!", "Third?"]
    assert sentences("") == []


def test_personality_varies_without_repeating(config):
    personality = Personality(config)
    greetings = {personality.greeting() for _ in range(6)}
    assert len(greetings) > 1


def test_personality_does_not_say_sir_every_time(config):
    config.personality.honorific_frequency = 0.0
    personality = Personality(config)
    replies = [personality.acknowledgement() for _ in range(12)]
    assert any("sir" not in reply.lower() for reply in replies)


def test_personality_respects_a_custom_honorific(config):
    config.personality.address_user_as = "boss"
    config.personality.honorific_frequency = 1.0
    personality = Personality(config)
    assert "sir" not in " ".join(personality.wake_response() for _ in range(8)).lower()


def test_automation_acknowledgements_come_from_their_own_kind_specific_list(config):
    """acknowledgement() applies probabilistic honorific substitution
    (Personality._apply_honorific), so the returned text won't always
    match an AUTOMATION_ACKS entry verbatim — check for phrasing unique to
    that list (never present in the generic ACKNOWLEDGEMENTS list) instead
    of exact membership."""
    personality = Personality(config)
    replies = [personality.acknowledgement(long_running=True, kind="automation")
              for _ in range(12)]
    assert all(any(word in reply for word in ("narrate", "posted", "talk you through"))
              for reply in replies), replies


def test_system_prompt_carries_identity_and_context(config):
    personality = Personality(config)
    prompt = personality.system_prompt("Known preferences:\n- tea over coffee")
    assert "JARVIS" in prompt and "British" in prompt
    assert "tea over coffee" in prompt


def test_engine_selection_falls_back_off_macos(config, app):
    config.voice.tts_engine = "browser"
    engine = build_tts(config, app.bus)
    assert isinstance(engine, BrowserTTS)
    config.voice.tts_engine = "off"
    assert isinstance(build_tts(config, app.bus), NullTTS)


async def test_null_engines_report_why_they_are_unavailable(config):
    config.voice.stt_engine = "off"
    config.voice.wake_engine = "off"
    stt = build_stt(config)
    wake = build_wake_detector(config, stt)
    ok, note = await stt.available()
    assert ok is False and "disabled" in note
    ok, note = await wake.available()
    assert ok is False and note


async def test_browser_tts_publishes_speech_events(app, config):
    config.voice.enabled = True
    engine = BrowserTTS(app.bus, "Daniel")
    await engine.speak("Good evening.")
    types = [e.type for e in app.bus.history]
    assert "speech.start" in types and "speech.end" in types


async def test_voice_manager_probe_reports_each_component(app, config):
    manager = VoiceManager(config, app.bus, app.telemetry)
    status = await manager.probe()
    for part in ("microphone", "stt", "tts", "wake"):
        assert part in status
        assert "ok" in status[part]
    # Without a local microphone the UI is told to fall back to the browser.
    assert status["browser_fallback"] in {True, False}


async def test_voice_manager_start_degrades_without_a_microphone(app, config):
    manager = VoiceManager(config, app.bus, app.telemetry)
    config.voice.enabled = True
    started = await manager.start()
    # On a machine without audio capture this must be a clean False, not a crash.
    assert started in {True, False}
    if not started:
        assert manager.state == VoiceState.OFF
    await manager.stop()


async def test_concurrent_start_creates_only_one_listen_loop(app, config, monkeypatch):
    """A real macOS runtime report described a browser action apparently
    running several times for one spoken request, though the route log
    showed only a single decision — consistent with two capture/dispatch
    pipelines racing on the same microphone. start() used to check "already
    listening" only *after* awaiting probe(), so two overlapping calls (the
    automatic startup call and a user-triggered "voice.start" arriving
    while it was still probing, say) could both pass that check before
    either had set _listen_task. Fixed by moving the check inside a lock,
    before the await."""
    from jarvis.voice.audio import Microphone

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)

    async def ok():
        return True, "ok"

    monkeypatch.setattr(Microphone, "available", staticmethod(lambda: (True, "ok")))
    monkeypatch.setattr(manager.stt, "available", ok)
    monkeypatch.setattr(manager.wake, "available", ok)

    entered = 0
    release = asyncio.Event()

    async def fake_listen_loop():
        nonlocal entered
        entered += 1
        await release.wait()

    monkeypatch.setattr(manager, "_listen_loop", fake_listen_loop)

    results = await asyncio.gather(manager.start(), manager.start(), manager.start())
    await asyncio.sleep(0.05)

    assert results == [True, True, True]
    assert entered == 1, f"_listen_loop was entered {entered} time(s), expected exactly 1"

    release.set()
    await manager.stop()


async def test_speech_queue_can_be_interrupted(app, config):
    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _SlowTTS()
    manager.enqueue("First sentence.")
    manager.enqueue("Second sentence.")
    await asyncio.sleep(0.05)
    await manager.stop_speaking()
    await asyncio.sleep(0.05)
    assert manager.speaking is False
    assert manager.tts.stopped is True


async def test_barge_in_stops_speech_before_routing(app, config, monkeypatch):
    """Speaking must stop the instant the user says something new."""
    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _SlowTTS()
    app.orchestrator.voice = manager
    asyncio.create_task(manager.speak("A long spoken answer that should be interrupted."))
    await asyncio.sleep(0.05)
    await app.ask("stop")
    assert manager.tts.stopped is True


class _SlowTTS:
    name = "slow"

    def __init__(self):
        self.stopped = False
        self.spoken: list[str] = []

    async def available(self):
        return True, "ok"

    async def speak(self, text):
        self.spoken.append(text)
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        return True

    async def stop(self):
        self.stopped = True
        return True

    async def voices(self):
        return []


async def test_speaking_never_blocks_the_turn(app, config):
    """A reply is finished when the answer exists, not when speech has played."""
    import time

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _SlowTTS()          # each utterance takes two seconds
    app.orchestrator.voice = manager

    started = time.perf_counter()
    result = await app.ask("what time is it?")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert result.text.startswith("It's")
    assert elapsed_ms < 500, f"the turn waited for speech ({elapsed_ms:.0f} ms)"
    await asyncio.sleep(0.05)
    assert manager.tts.spoken, "the reply was never queued for speech"
    await manager.stop_speaking()


async def test_queued_sentences_are_spoken_in_order(app, config):
    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _FastTTS()
    manager.enqueue("First.")
    manager.enqueue("Second.")
    await asyncio.sleep(0.2)
    assert manager.tts.spoken == ["First.", "Second."]


class _FastTTS(_SlowTTS):
    async def speak(self, text):
        self.spoken.append(text)
        await asyncio.sleep(0.01)
        return True
