"""Microphone capture and PCM conversion.

A single input stream feeds both the wake-word detector and utterance capture,
so the microphone is opened once and shared. Voice activity is detected with a
simple RMS gate — cheap, predictable, and adequate given Whisper's own VAD runs
on the captured segment afterwards.

**The frame contract.** ``sounddevice``'s ``RawInputStream`` hands back a raw
CFFI buffer, which we copy into ``bytes`` so it can cross a thread queue safely.
Every consumer therefore receives *raw native-endian 16-bit mono PCM at 16 kHz*
and must convert before use: openWakeWord requires ``int16`` samples in a 1-D
numpy array, Whisper requires ``float32`` in [-1, 1]. Those conversions live
here, in one place, instead of being re-implemented — or forgotten — by each
consumer.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
from typing import TYPE_CHECKING, Any

from ..core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - numpy ships with the optional voice extras
    import numpy as np

log = get_logger("jarvis.voice.audio")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms — openWakeWord's native chunk size
#: Bytes per sample of 16-bit PCM.
SAMPLE_WIDTH = 2


def _numpy():
    """Import numpy lazily: it is part of the optional voice extras."""
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - only without the voice extras
        raise RuntimeError(
            "numpy is required for audio processing. Install the voice extras: "
            'pip install -e ".[voice]"'
        ) from exc
    return np


def to_int16_frame(frame: Any) -> np.ndarray:
    """Convert a captured frame into the array openWakeWord requires.

    Returns a **1-D, C-contiguous, writable, native-endian int16** array of mono
    samples, which is what openWakeWord 0.6 needs:

    * ``Model.predict`` raises ``ValueError`` for anything that is not an
      ``ndarray`` — passing raw ``bytes`` is what broke V1.0.
    * ``int16`` is not merely preferred. The streaming path buffers each frame
      through ``list`` and then ``np.array(...).astype(np.int16)``, so float
      samples in [-1, 1] are *silently truncated to zero* rather than rejected:
      the detector would run happily and never fire.
    * The streaming buffer concatenates each frame with a 1-D remainder, so a
      2-D ``(frames, channels)`` array breaks its arithmetic.

    Accepts raw PCM (``bytes``/``bytearray``/``memoryview``) as produced by the
    microphone, or an existing array in int16, float (assumed to be in [-1, 1])
    or another integer dtype. Multi-channel input is down-mixed to mono.
    """
    np = _numpy()

    if isinstance(frame, (bytes, bytearray, memoryview)):
        raw = bytes(frame)
        # A partial sample can only come from a truncated buffer. Drop it rather
        # than let np.frombuffer raise on a length that isn't a whole number of
        # samples.
        extra = len(raw) % SAMPLE_WIDTH
        if extra:
            log.debug("dropping %d trailing byte(s) from a partial PCM frame", extra)
            raw = raw[: len(raw) - extra]
        # frombuffer returns a read-only view onto the bytes object; copy so the
        # array owns writable memory. openWakeWord keeps a view of each frame's
        # tail between calls, so it must not alias a buffer we might reuse.
        return np.frombuffer(raw, dtype=np.int16).copy()

    if not isinstance(frame, np.ndarray):
        raise TypeError(f"unsupported audio frame type: {type(frame).__name__}")

    samples = frame
    # Scale into the int16 domain *before* down-mixing: averaging channels
    # produces a float array whose values are already int16-ranged, and scaling
    # that again would clip every sample to the rails.
    if np.issubdtype(samples.dtype, np.floating):
        samples = np.clip(samples, -1.0, 1.0) * 32767.0
    if samples.ndim > 1:  # (frames, channels) → mono
        samples = samples.mean(axis=tuple(range(1, samples.ndim)))
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    if samples.flags.writeable and samples.flags.c_contiguous:
        return samples
    return np.ascontiguousarray(samples, dtype=np.int16).copy()


def to_float32_frame(frame: Any) -> np.ndarray:
    """Convert a captured frame to float32 in [-1, 1] — what Whisper expects."""
    np = _numpy()
    if isinstance(frame, np.ndarray) and frame.dtype == np.float32 and frame.ndim == 1:
        return frame
    return to_int16_frame(frame).astype(np.float32) / 32768.0


class Microphone:
    def __init__(self, device: str | int | None = None, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.device = device or None
        self._stream = None
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._running = False

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            import sounddevice  # noqa: F401
        except (ImportError, OSError) as exc:
            return False, f"sounddevice isn't usable ({exc})"
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            if not any(d.get("max_input_channels", 0) > 0 for d in devices):
                return False, "no audio input device was found"
        except Exception as exc:  # pragma: no cover - platform dependent
            return False, f"audio devices couldn't be queried ({exc})"
        return True, "ok"

    def start(self) -> None:
        if self._running:
            return
        import sounddevice as sd

        def callback(indata, frames, time_info, status):  # pragma: no cover - realtime thread
            if status:
                log.debug("audio status: %s", status)
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate, blocksize=FRAME, dtype="int16", channels=1,
            device=self.device, callback=callback,
        )
        self._stream.start()
        self._running = True
        log.info("microphone open at %d Hz", self.sample_rate)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # pragma: no cover
                pass
            self._stream = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    @property
    def running(self) -> bool:
        return self._running

    async def frames(self):
        """Yield 80 ms frames of raw 16-bit mono PCM as ``bytes``.

        Consumers convert with :func:`to_int16_frame` or
        :func:`to_float32_frame` — see the module docstring.
        """
        while self._running:
            try:
                yield await asyncio.to_thread(self._queue.get, True, 0.5)
            except queue.Empty:
                continue

    def drain(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


def rms(frame: Any) -> float:
    """Signal level of a frame, 0.0–1.0, for the voice-activity gate."""
    try:
        np = _numpy()
        samples = to_float32_frame(frame)
    except (RuntimeError, TypeError, ValueError):
        return 0.0
    if not samples.size:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


async def record_utterance(microphone: Microphone, *, silence_threshold: float = 0.012,
                           silence_tail_s: float = 0.9, max_seconds: float = 20.0,
                           on_level=None, cancel_event: asyncio.Event | None = None):
    """Record until the speaker stops. Returns float32 audio (or None)."""
    try:
        _numpy()
    except RuntimeError:
        return None

    collected: list[bytes] = []
    started = asyncio.get_event_loop().time()
    last_voice = started
    heard_speech = False

    async for frame in microphone.frames():
        if cancel_event is not None and cancel_event.is_set():
            return None
        now = asyncio.get_event_loop().time()
        level = rms(frame)
        if on_level is not None:
            on_level(level)
        if level > silence_threshold:
            heard_speech = True
            last_voice = now
            collected.append(frame)
        elif heard_speech:
            collected.append(frame)
            if now - last_voice > silence_tail_s:
                break
        elif now - started > 3.0:
            return None  # nobody spoke
        if now - started > max_seconds:
            break

    if not collected:
        return None
    return to_float32_frame(b"".join(collected))
