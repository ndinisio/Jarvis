"""Wake-word detection.

Two offline strategies, chosen by configuration:

``openwakeword``  a small neural detector with a pretrained "hey jarvis" model.
                  Continuous, low CPU, ~200 ms latency.
``whisper``       energy-gated chunks transcribed by the local Whisper model and
                  matched against the wake word. No extra dependency, slightly
                  slower, and useful when a custom wake word is wanted.

Both run entirely on the machine — audio never leaves the Mac to decide whether
the user said "Jarvis".

**Input contract.** :meth:`WakeWordDetector.process` takes one frame of 16 kHz
mono audio, either as raw PCM ``bytes`` straight off the microphone or as a
numpy array. Detectors convert with :func:`jarvis.voice.audio.to_int16_frame`
rather than assuming a representation: openWakeWord requires a 1-D ``int16``
array and raises on anything else.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import os
import time
from typing import Any

from ..core.logging import get_logger
from .audio import to_float32_frame, to_int16_frame

log = get_logger("jarvis.voice.wake")

CHUNK = 1280  # 80 ms at 16 kHz — openWakeWord's native frame size


class WakeWordDetector(abc.ABC):
    name = "wake"

    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    def process(self, frame: Any) -> float:
        """Score one frame of 16 kHz mono audio, 0..1.

        *frame* is raw PCM ``bytes`` from the microphone or a numpy array;
        implementations convert it themselves via
        :func:`jarvis.voice.audio.to_int16_frame`.
        """

    async def prepare(self) -> None:
        """Load models before the listening loop starts.

        Doing this up front means a missing model or an unusable runtime fails
        at start-up, with a message, instead of raising inside the frame loop.
        """
        return None

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
        """Fetch the pretrained model if needed and build the detector.

        openWakeWord resolves a bare name like ``hey_jarvis`` against its
        bundled ``MODELS`` table by substring, but only *after* the file has
        been downloaded — a missing file surfaces later as an opaque runtime
        error, so the download is done first and checked.
        """
        import openwakeword
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        model_key = self.BUILTIN[self.wake_word]
        try:  # pragma: no cover - network side effect on first run
            download_models([model_key])
        except Exception as exc:
            log.warning("could not download the %s wake model: %s", model_key, exc)

        expected = [
            path for path in openwakeword.get_pretrained_model_paths("onnx")
            if model_key in path
        ]
        if expected and not os.path.exists(expected[0]):
            raise FileNotFoundError(
                f"the {model_key} wake-word model is missing at {expected[0]}. "
                "Run `python -c \"from openwakeword.utils import download_models; "
                f'download_models([\'{model_key}\'])"` to fetch it.'
            )

        log.info("loading wake-word model %s (onnx)", model_key)
        return Model(wakeword_models=[model_key], inference_framework="onnx")

    async def prepare(self) -> None:
        if self._model is None:
            # Loading touches the network and onnxruntime; keep it off the loop.
            self._model = await asyncio.to_thread(self._load)

    def process(self, frame: Any) -> float:
        if self._model is None:
            self._model = self._load()
        # openWakeWord requires a 1-D int16 numpy array. The microphone hands us
        # raw PCM bytes, so the conversion happens here rather than being left
        # to the caller (passing the bytes through is what broke V1.0).
        samples = to_int16_frame(frame)
        scores = self._model.predict(samples)
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
        samples = to_float32_frame(frame)
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
