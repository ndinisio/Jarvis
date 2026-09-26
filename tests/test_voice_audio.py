"""Audio-format regression tests (V1.1).

V1.0 passed raw PCM ``bytes`` from the microphone straight into
``openwakeword.Model.predict``, which crashed the wake-word loop with::

    ValueError: The input audio data (x) must by a Numpy array,
    instead received an object of type <class 'bytes'>.

These tests pin the conversion at the audio boundary. ``_FakeOWWModel``
deliberately reproduces openWakeWord 0.6's real behaviour — including the part
that is *not* an exception — so the bug cannot come back in a quieter form.
"""

from __future__ import annotations

import asyncio

import pytest
from jarvis.voice.audio import FRAME, SAMPLE_RATE, rms, to_float32_frame, to_int16_frame
from jarvis.voice.wakeword import OpenWakeWordDetector, WhisperWakeDetector

# numpy ships with the optional voice extras, so a base install skips this
# module rather than failing to collect it.
np = pytest.importorskip("numpy", reason="numpy is installed with the voice extras")


def pcm_bytes(samples: int = FRAME, amplitude: int = 6000, seed: int = 0) -> bytes:
    """One microphone frame: 16-bit mono PCM, exactly as sounddevice yields it."""
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amplitude, samples)).astype(np.int16).tobytes()


class _FakeOWWModel:
    """Stands in for ``openwakeword.Model``, enforcing its real contract.

    Mirrors openwakeword 0.6.0:

    * ``model.py`` rejects a non-ndarray with that exact ValueError;
    * the streaming path runs ``np.array(x.tolist()).astype(np.int16)``, which
      turns float samples in [-1, 1] into silence instead of raising — so this
      fake reports that as "deaf" rather than pretending it worked;
    * the buffer concatenates with a 1-D remainder, so 2-D input is invalid.
    """

    def __init__(self):
        self.received: list[np.ndarray] = []
        self.deaf_frames = 0

    def predict(self, x, **_kwargs):
        if not isinstance(x, np.ndarray):
            raise ValueError(
                "The input audio data (x) must by a Numpy array, "
                f"instead received an object of type {type(x)}."
            )
        if x.ndim != 1:
            raise ValueError(f"streaming buffer needs 1-D audio, got shape {x.shape}")
        self.received.append(x)
        coerced = np.array(x.tolist()).astype(np.int16)
        if x.size and not coerced.any():
            # Exactly what float32 input does to openWakeWord: no error, no audio.
            self.deaf_frames += 1
        return {"hey_jarvis": 0.0}


# --- the conversion itself ---------------------------------------------------

def test_microphone_bytes_become_the_array_openwakeword_requires():
    frame = pcm_bytes()
    samples = to_int16_frame(frame)

    assert isinstance(samples, np.ndarray)
    assert samples.dtype == np.int16
    assert samples.ndim == 1
    assert samples.shape == (FRAME,)
    assert samples.flags.c_contiguous
    # openWakeWord keeps a view of each frame's tail between calls, so the array
    # must own writable memory rather than alias an immutable bytes object.
    assert samples.flags.writeable


def test_conversion_preserves_the_samples():
    original = np.array([0, 1, -1, 32767, -32768, 1234], dtype=np.int16)
    assert np.array_equal(to_int16_frame(original.tobytes()), original)


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview])
def test_all_buffer_types_convert(wrap):
    frame = pcm_bytes()
    assert to_int16_frame(wrap(frame)).shape == (FRAME,)


def test_partial_sample_is_trimmed_not_fatal():
    """A truncated buffer must not raise inside the listening loop."""
    assert to_int16_frame(pcm_bytes() + b"\x01").shape == (FRAME,)


def test_float_input_is_scaled_not_truncated():
    """The naive fix — float32 in [-1, 1] — would be silently destroyed."""
    quiet = np.array([0.5, -0.5, 0.0], dtype=np.float32)
    converted = to_int16_frame(quiet)
    assert converted.dtype == np.int16
    assert converted[0] > 16000 and converted[1] < -16000


def test_float_input_is_clipped():
    converted = to_int16_frame(np.array([2.0, -2.0], dtype=np.float32))
    assert converted[0] == 32767 and converted[1] == -32767


def test_multichannel_is_downmixed_to_mono():
    stereo = np.zeros((FRAME, 2), dtype=np.int16)
    stereo[:, 0] = 1000
    stereo[:, 1] = 3000
    mono = to_int16_frame(stereo)
    assert mono.shape == (FRAME,)
    assert mono[0] == 2000


def test_readonly_array_is_made_writable():
    readonly = np.frombuffer(pcm_bytes(), dtype=np.int16)
    assert not readonly.flags.writeable
    assert to_int16_frame(readonly).flags.writeable


def test_correct_array_passes_through_without_copying():
    samples = np.zeros(FRAME, dtype=np.int16)
    assert to_int16_frame(samples) is samples


def test_unsupported_input_raises_clearly():
    with pytest.raises(TypeError, match="unsupported audio frame type"):
        to_int16_frame("not audio")


def test_float32_conversion_for_whisper():
    samples = to_float32_frame(pcm_bytes())
    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert samples.min() >= -1.0
    assert samples.max() <= 1.0


def test_rms_gate_reads_levels_from_raw_bytes():
    assert rms(bytes(FRAME * 2)) == 0.0
    assert rms(pcm_bytes(amplitude=8000)) > 0.1
    assert rms("not audio") == 0.0          # never raises inside the capture loop


def test_frame_geometry_matches_openwakeword():
    """80 ms at 16 kHz is openWakeWord's native chunk."""
    assert FRAME == 1280
    assert SAMPLE_RATE == 16000
    assert abs(FRAME / SAMPLE_RATE - 0.08) < 1e-9


# --- the detector boundary ---------------------------------------------------

def test_detector_never_passes_bytes_to_the_model():
    """The V1.0 regression, pinned."""
    detector = OpenWakeWordDetector("jarvis")
    detector._model = _FakeOWWModel()

    detector.process(pcm_bytes())          # raw bytes, as the microphone yields

    assert detector._model.received, "predict() was never called"
    handed_over = detector._model.received[0]
    assert isinstance(handed_over, np.ndarray)
    assert handed_over.dtype == np.int16
    assert handed_over.ndim == 1


def test_detector_accepts_an_already_converted_array():
    """The manager converts once at the boundary; that must not double-convert."""
    detector = OpenWakeWordDetector("jarvis")
    detector._model = _FakeOWWModel()
    samples = to_int16_frame(pcm_bytes())

    detector.process(samples)

    assert detector._model.received[0] is samples


def test_detector_audio_is_not_silently_deafened():
    """Guards the failure mode that raises nothing: audio arriving as zeros."""
    detector = OpenWakeWordDetector("jarvis")
    detector._model = _FakeOWWModel()

    for _ in range(5):
        detector.process(pcm_bytes(amplitude=6000))

    assert detector._model.deaf_frames == 0, "audio reached the model as silence"


def test_detector_survives_a_long_run():
    detector = OpenWakeWordDetector("jarvis")
    detector._model = _FakeOWWModel()
    for index in range(200):               # 16 seconds of audio
        assert detector.process(pcm_bytes(seed=index)) == 0.0
    assert len(detector._model.received) == 200


def test_detection_debounces_repeated_frames():
    class _Hot(_FakeOWWModel):
        def predict(self, x, **kwargs):
            super().predict(x, **kwargs)
            return {"hey_jarvis": 0.99}

    detector = OpenWakeWordDetector("jarvis", sensitivity=0.5)
    detector._model = _Hot()
    assert detector.process(pcm_bytes()) > 0.9      # fires
    assert detector.process(pcm_bytes()) == 0.0     # debounced, not a second wake


class _FakeVAD:
    """Stands in for openwakeword.vad.VAD — a real speech/non-speech
    classifier, unlike the volume threshold it's preferred over."""

    def __init__(self, score: float = 1.0):
        self.score = score
        self.received: list[np.ndarray] = []

    def predict(self, x, frame_size=640):
        self.received.append(x)
        return self.score


class _BrokenVAD:
    def __init__(self):
        self.calls = 0

    def predict(self, x, frame_size=640):
        self.calls += 1
        raise RuntimeError("onnxruntime blew up")


class _StubSTT:
    async def available(self):
        return True, "ok"

    async def transcribe(self, audio, sample_rate=16000):
        return ""


def test_whisper_detector_uses_the_same_conversion():
    detector = WhisperWakeDetector(_StubSTT(), "jarvis")
    detector._vad = _FakeVAD(score=1.0)
    detector.process(pcm_bytes(amplitude=9000))
    assert detector._buffer, "speech the VAD recognises should have been buffered"
    assert detector._buffer[0].dtype == np.float32
    assert detector._vad.received, "the VAD must see the int16 frame, not bytes"


def test_whisper_detector_prefers_the_vad_over_volume_alone():
    """Quiet audio the VAD nonetheless calls speech must be buffered — the
    volume threshold is only a fallback, not consulted when the VAD works."""
    detector = WhisperWakeDetector(_StubSTT(), "jarvis", energy_threshold=0.5)
    detector._vad = _FakeVAD(score=0.9)
    detector.process(pcm_bytes(amplitude=10))
    assert detector._buffer


def test_whisper_detector_ignores_loud_audio_the_vad_calls_non_speech():
    """A loud non-speech sound (a door, a fan) must not fool the gate just
    because a volume-only check would have let it through."""
    detector = WhisperWakeDetector(_StubSTT(), "jarvis", energy_threshold=0.001)
    detector._vad = _FakeVAD(score=0.1)
    detector.process(pcm_bytes(amplitude=9000))
    assert not detector._buffer


def test_whisper_detector_falls_back_to_the_volume_threshold_if_the_vad_breaks():
    detector = WhisperWakeDetector(_StubSTT(), "jarvis", energy_threshold=0.001)
    broken = _BrokenVAD()
    detector._vad = broken
    detector.process(pcm_bytes(amplitude=9000))
    assert detector._buffer, "a broken VAD must fall back to the volume threshold, not go silent"
    assert broken.calls == 1
    assert detector._vad_broken is True

    detector.process(pcm_bytes(amplitude=9000))
    assert broken.calls == 1, "a VAD already known to be broken must not be retried every frame"


# --- the listening loop ------------------------------------------------------

class _FakeMicrophone:
    """A microphone that yields a fixed number of real PCM frames."""

    def __init__(self, frames: int = 10):
        self.sample_rate = SAMPLE_RATE
        self._count = frames
        self.running = True
        self.drained = 0

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def drain(self):
        self.drained += 1

    async def frames(self):
        for index in range(self._count):
            yield pcm_bytes(seed=index)


class _RecordingDetector:
    """Records exactly what the manager hands it."""

    name = "recording"
    threshold = 0.5

    def __init__(self, fire_on: int | None = None, raise_times: int = 0):
        self.seen: list = []
        self._fire_on = fire_on
        self._raise_times = raise_times
        self.calls = 0

    async def available(self):
        return True, "ok"

    async def prepare(self):
        return None

    def process(self, frame):
        self.calls += 1
        if self._raise_times >= self.calls:
            raise RuntimeError("detector exploded")
        self.seen.append(frame)
        if self._fire_on is not None and self.calls >= self._fire_on:
            return 0.95
        return 0.0

    def reset(self):
        return None


def _manager(app, config, detector, frames=10):
    from jarvis.voice.manager import VoiceManager

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.wake = detector
    manager.microphone = _FakeMicrophone(frames)
    return manager


async def test_wake_loop_hands_the_detector_a_numpy_array(app, config):
    """The V1.0 crash was the manager passing bytes straight through."""
    detector = _RecordingDetector(fire_on=3)
    manager = _manager(app, config, detector)

    assert await manager._await_wake() is True

    assert detector.seen, "the detector was never called"
    for frame in detector.seen:
        assert isinstance(frame, np.ndarray)
        assert frame.dtype == np.int16
        assert frame.ndim == 1
    wake_events = [e for e in app.bus.history if e.type == "wake"]
    assert wake_events and wake_events[-1].payload["score"] == pytest.approx(0.95)


async def test_wake_loop_survives_a_transient_detector_error(app, config):
    """A fault must be reported and survived, not swallowed and not fatal."""
    detector = _RecordingDetector(fire_on=4, raise_times=2)
    manager = _manager(app, config, detector)

    assert await manager._await_wake() is True          # kept listening

    errors = [e for e in app.bus.history if e.type == "error"]
    assert errors, "the failure was swallowed silently"
    assert "still listening" in errors[0].payload["message"]
    assert "detector exploded" in errors[0].payload["detail"]


async def test_wake_loop_gives_up_loudly_after_persistent_failure(app, config):
    from jarvis.voice.manager import _MAX_WAKE_FAILURES

    detector = _RecordingDetector(raise_times=99)
    manager = _manager(app, config, detector, frames=50)

    with pytest.raises(RuntimeError, match="detector exploded"):
        await manager._await_wake()

    assert detector.calls == _MAX_WAKE_FAILURES
    errors = [e.payload["message"] for e in app.bus.history if e.type == "error"]
    assert any("stopped" in message for message in errors)


async def test_wake_model_is_loaded_before_the_loop_starts(app, config):
    """A model that can't load must fail at start-up, not inside the hot loop."""
    prepared = {"called": False}

    class _FailingDetector(_RecordingDetector):
        async def prepare(self):
            prepared["called"] = True
            raise FileNotFoundError("the hey_jarvis model is missing")

    manager = _manager(app, config, _FailingDetector())

    async def _stop_after_one_pass():
        # The loop falls through to capture when the wake word is unavailable;
        # end it there rather than letting `while True` spin.
        raise asyncio.CancelledError

    manager._capture_and_dispatch = _stop_after_one_pass
    with pytest.raises(asyncio.CancelledError):
        await manager._listen_loop()

    assert prepared["called"] is True
    errors = [e.payload for e in app.bus.history if e.type == "error"]
    assert any("wake word couldn't be loaded" in e["message"] for e in errors)
    assert any("hey_jarvis model is missing" in (e.get("detail") or "") for e in errors)
    # Listening continues without the wake word rather than shutting down.
    assert manager.microphone.running is False   # the loop closed the device cleanly
