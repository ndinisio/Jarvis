"""Voice: speech shaping, engine selection and graceful degradation."""

from __future__ import annotations

import asyncio

from jarvis.core.personality import Personality, sentences, speakable
from jarvis.voice.manager import VoiceManager, VoiceState
from jarvis.voice.stt import build_stt
from jarvis.voice.tts import BrowserTTS, KokoroTTS, NullTTS, build_tts
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


def test_engine_selection_builds_kokoro_with_its_own_config(config, app):
    config.voice.tts_engine = "kokoro"
    config.voice.tts_voice = "bm_lewis"
    config.voice.kokoro_model_path = "/tmp/kokoro-v1.0.onnx"
    config.voice.kokoro_voices_path = "/tmp/voices-v1.0.bin"
    engine = build_tts(config, app.bus)
    assert isinstance(engine, KokoroTTS)
    assert engine.voice == "bm_lewis"
    assert engine.model_path == "/tmp/kokoro-v1.0.onnx"
    assert engine.voices_path == "/tmp/voices-v1.0.bin"


async def test_kokoro_reports_the_package_is_missing(config):
    """kokoro-onnx isn't part of the base install — available() must say so
    plainly rather than raising, the same contract every other optional
    engine (faster-whisper, whispercpp, openwakeword) honours."""
    engine = KokoroTTS(model_path="/tmp/model.onnx", voices_path="/tmp/voices.bin")
    ok, note = await engine.available()
    assert ok is False
    assert "kokoro-onnx" in note and "installed" in note


def _fake_kokoro_and_sounddevice(monkeypatch):
    """available() now also checks sounddevice importability (a real gap
    found in verification: following the tool's own printed install
    instruction, `pip install -e ".[kokoro]"` alone, leaves sounddevice
    missing and every call failing deep inside _play_blocking()). Tests that
    exercise the path-existence checks need both faked so they reach that
    logic instead of stopping at the sounddevice check."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.ModuleType("kokoro_onnx"))
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))


async def test_kokoro_reports_missing_model_files(monkeypatch):
    """With the package importable but no model/voices path configured,
    available() must name which path is missing rather than failing deeper
    inside (e.g. when kokoro-onnx is installed for STT use elsewhere but the
    TTS model itself was never downloaded)."""
    _fake_kokoro_and_sounddevice(monkeypatch)

    engine = KokoroTTS()
    ok, note = await engine.available()
    assert ok is False and "kokoro_model_path" in note

    engine.model_path = "/tmp/model.onnx"  # still missing on disk
    ok, note = await engine.available()
    assert ok is False and "kokoro_model_path" in note

    engine.model_path = __file__  # a real, existing file — good enough to pass the check
    ok, note = await engine.available()
    assert ok is False and "kokoro_voices_path" in note


async def test_kokoro_reports_missing_sounddevice(monkeypatch):
    """sounddevice genuinely isn't installed in this environment — confirms
    available() catches it explicitly (with an actionable message) rather
    than only failing later, deep inside _play_blocking() during a real
    speak() call."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.ModuleType("kokoro_onnx"))
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)

    engine = KokoroTTS(model_path=__file__, voices_path=__file__)
    ok, note = await engine.available()
    assert ok is False and "sounddevice" in note


async def test_kokoro_reports_a_directory_is_not_a_valid_model_path(monkeypatch):
    """A directory passed as kokoro_model_path used to pass available()
    (Path.exists() is true for directories too) and only broke later, deep
    inside _load(). is_file() catches it here instead."""
    from pathlib import Path

    _fake_kokoro_and_sounddevice(monkeypatch)

    engine = KokoroTTS(model_path=str(Path(__file__).parent), voices_path=__file__)
    ok, note = await engine.available()
    assert ok is False and "kokoro_model_path" in note


async def test_kokoro_speak_calls_are_serialized(monkeypatch):
    """Two overlapping speak() calls (e.g. the /api/voice/speak route firing
    mid-drain of a streamed reply) must not interleave — without a lock,
    each spawns a worker thread writing the same self._stream."""
    import sys
    import time as time_module
    import types

    order: list[str] = []

    class FakeModel:
        def create(self, text, voice=None, speed=None, lang=None):
            order.append(f"synth-start:{text}")
            time_module.sleep(0.02)
            order.append(f"synth-end:{text}")
            return [0.0, 0.0], 24000

    class FakeStream:
        def __init__(self, **kwargs):
            pass

        def start(self):
            order.append("stream-start")

        def write(self, samples):
            order.append("stream-write")

        def stop(self):
            pass

        def close(self):
            pass

        def abort(self):  # pragma: no cover - not exercised in this test
            pass

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.OutputStream = lambda **kwargs: FakeStream(**kwargs)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    engine = KokoroTTS()
    engine._model = FakeModel()  # skip warmup/_load — not what this test is about

    results = await asyncio.gather(engine.speak("first"), engine.speak("second"))
    assert results == [True, True]
    # If the lock is doing its job, the first call's entire synth→play
    # sequence finishes before the second call's synthesis ever starts —
    # never interleaved.
    first_write = order.index("stream-write")
    second_start = order.index("synth-start:second")
    assert first_write < second_start, f"calls interleaved: {order}"


async def test_kokoro_stop_during_synthesis_prevents_playback(monkeypatch):
    """Barge-in during the (CPU-bound, potentially multi-second) synthesis
    phase used to be a complete no-op — self._stream didn't exist yet, so
    stop() saw nothing to interrupt and the utterance played out in full
    once synthesis finished."""
    import sys
    import time as time_module
    import types

    played: list[str] = []

    class FakeModel:
        def create(self, text, voice=None, speed=None, lang=None):
            time_module.sleep(0.05)
            return [0.0], 24000

    class FakeStream:
        def __init__(self, **kwargs):
            pass

        def start(self):
            played.append("start")

        def write(self, samples):
            played.append("write")

        def stop(self):
            pass

        def close(self):
            pass

        def abort(self):
            pass

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.OutputStream = lambda **kwargs: FakeStream(**kwargs)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    engine = KokoroTTS()
    engine._model = FakeModel()

    speak_task = asyncio.create_task(engine.speak("hello"))
    await asyncio.sleep(0.01)  # give speak() time to enter synthesis
    assert engine._speaking is True

    stopped = await engine.stop()
    assert stopped is True, "stop() must report something was interrupted during synthesis"

    result = await speak_task
    assert result is False
    assert played == [], "playback must never start once stop() fired during synthesis"


async def test_kokoro_warmup_failure_does_not_propagate_out_of_speak(monkeypatch):
    """An uncaught warmup() exception used to escape speak() entirely — via
    the wake-ack path that's only caught by VoiceManager._listen_loop()'s
    outer handler, tearing down the whole voice loop over an isolated TTS
    problem (e.g. a corrupted model file)."""

    async def broken_warmup():
        raise RuntimeError("corrupted model file")

    engine = KokoroTTS(model_path="/tmp/model.onnx", voices_path="/tmp/voices.bin")
    monkeypatch.setattr(engine, "warmup", broken_warmup)

    result = await engine.speak("hello")
    assert result is False
    assert engine._speaking is False


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
