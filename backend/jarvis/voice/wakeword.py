"""Wake-word detection.

Two offline strategies, chosen by configuration:

``openwakeword``  a small neural detector with a pretrained "hey jarvis" model.
                  Continuous, low CPU, ~200 ms latency.
``whisper``       energy-gated chunks transcribed by the local Whisper model and
                  matched against the wake word. No extra dependency, slightly
                  slower, and useful when a custom wake word is wanted.

Both run entirely on the machine — audio never leaves the Mac to decide whether
the user said "Jarvis".
"""

from __future__ import annotations

import abc
import contextlib
import time
from typing import Any

from ..core.logging import get_logger

log = get_logger("jarvis.voice.wake")

CHUNK = 1280  # 80 ms at 16 kHz — openWakeWord's native frame size


class WakeWordDetector(abc.ABC):
    name = "wake"

    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    def process(self, frame: Any) -> float:
        """Feed one frame of int16 audio; return a detection score in 0..1."""

    def reset(self) -> None:
        return None

    @property
    def threshold(self) -> float:
        return 0.5


class OpenWakeWordDetector(WakeWordDetector):
    name = "openwakeword"

    #: Pretrained models that ship with openWakeWord, mapped from wake phrase.
    BUILTIN = {
        "jarvis": "hey_jarvis", "hey jarvis": "hey_jarvis", "alexa": "alexa",
        "hey mycroft": "hey_mycroft", "hey rhasspy": "hey_rhasspy",
    }

    def __init__(self, wake_word: str = "jarvis", sensitivity: float = 0.5):
        self.wake_word = (wake_word or "jarvis").lower().strip()
        self.sensitivity = sensitivity
        self._model = None
        self._last_fire = 0.0

    @property
    def threshold(self) -> float:
        # Higher sensitivity → lower score needed.
        return max(0.15, min(0.95, 1.0 - self.sensitivity))

    async def available(self) -> tuple[bool, str]:
        try:
            import openwakeword  # noqa: F401
        except ImportError:
            return False, "openwakeword isn't installed (pip install openwakeword)"
        if self.wake_word not in self.BUILTIN:
            return False, (f"no pretrained model for “{self.wake_word}”; "
                           "use the whisper wake engine or train a model")
        return True, "ok"

    def _load(self):
        from openwakeword.model import Model

        try:  # pragma: no cover - first-run model download
            import openwakeword

            openwakeword.utils.download_models([self.BUILTIN[self.wake_word]])
        except Exception as exc:
            log.debug("openwakeword model download skipped: %s", exc)
        return Model(wakeword_models=[self.BUILTIN[self.wake_word]], inference_framework="onnx")

    def process(self, frame: Any) -> float:
        if self._model is None:
            self._model = self._load()
        scores = self._model.predict(frame)
        score = max(scores.values()) if scores else 0.0
        if score >= self.threshold:
            now = time.time()
            if now - self._last_fire < 1.5:  # debounce repeated frames
                return 0.0
            self._last_fire = now
        return float(score)

    def reset(self) -> None:
        if self._model is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                self._model.reset()


class WhisperWakeDetector(WakeWordDetector):
    """Energy-gated keyword spotting using the local STT model.

    Accumulates ~1.5 s of speech-level audio, transcribes it and looks for the
    wake word. Costs more CPU than openWakeWord but needs no extra dependency
    and supports any wake phrase.
    """

    name = "whisper"

    def __init__(self, stt, wake_word: str = "jarvis", threshold: float = 0.012):
        self._stt = stt
        self.wake_word = (wake_word or "jarvis").lower().strip()
        self._energy_threshold = threshold
        self._buffer: list[Any] = []
        self._pending: float = 0.0
        self.last_transcript = ""

    async def available(self) -> tuple[bool, str]:
        return await self._stt.available()

    def process(self, frame: Any) -> float:
        """Buffers audio; the manager calls :meth:`check` to do the real work."""
        try:
            import numpy as np
        except ImportError:
            return 0.0
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(samples))) if samples.size else 0.0)
        if rms > self._energy_threshold:
            self._buffer.append(samples)
            self._pending = time.time()
        elif self._buffer and time.time() - self._pending > 0.35:
            return 1.0  # a phrase has ended — ready to check
        if len(self._buffer) > 40:  # ~3 s
            return 1.0
        return 0.0

    async def check(self) -> bool:
        if not self._buffer:
            return False
        try:
            import numpy as np
        except ImportError:
            return False
        audio = np.concatenate(self._buffer)
        self._buffer.clear()
        if len(audio) < 4000:
            return False
        text = (await self._stt.transcribe(audio)).lower()
        self.last_transcript = text
        return self.wake_word in text.replace(",", " ").replace(".", " ")

    def reset(self) -> None:
        self._buffer.clear()


class NullWakeDetector(WakeWordDetector):
    name = "off"

    async def available(self) -> tuple[bool, str]:
        return False, "wake word detection is disabled"

    def process(self, frame: Any) -> float:
        return 0.0


def build_wake_detector(config: Any, stt) -> WakeWordDetector:
    voice = config.voice
    if voice.wake_engine == "openwakeword":
        return OpenWakeWordDetector(voice.wake_word, voice.wake_sensitivity)
    if voice.wake_engine == "whisper":
        return WhisperWakeDetector(stt, voice.wake_word, voice.silence_threshold)
    return NullWakeDetector()
