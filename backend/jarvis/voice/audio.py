"""Microphone capture.

A single input stream feeds both the wake-word detector and utterance capture,
so the microphone is opened once and shared. Voice activity is detected with a
simple RMS gate — cheap, predictable, and adequate given Whisper's own VAD runs
on the captured segment afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue

from ..core.logging import get_logger

log = get_logger("jarvis.voice.audio")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms


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
        """Yield 80 ms frames of int16 PCM."""
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


def rms(frame: bytes) -> float:
    try:
        import numpy as np
    except ImportError:
        return 0.0
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    if not samples.size:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


async def record_utterance(microphone: Microphone, *, silence_threshold: float = 0.012,
                           silence_tail_s: float = 0.9, max_seconds: float = 20.0,
                           on_level=None, cancel_event: asyncio.Event | None = None):
    """Record until the speaker stops. Returns float32 audio (or None)."""
    try:
        import numpy as np
    except ImportError:
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
    audio = np.frombuffer(b"".join(collected), dtype=np.int16).astype(np.float32) / 32768.0
    return audio
