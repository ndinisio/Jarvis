"""Speech to text.

Local Whisper by default (faster-whisper, which is CTranslate2 under the hood
and runs comfortably on Apple Silicon). whisper.cpp is supported for users who
already have it. Everything is optional: with no STT installed, JARVIS still
works by text and can fall back to the browser's own recogniser.
"""

from __future__ import annotations

import abc
import asyncio
import tempfile
import wave
from pathlib import Path
from typing import Any

from ..core.logging import get_logger

log = get_logger("jarvis.voice.stt")

SAMPLE_RATE = 16000


class STTEngine(abc.ABC):
    name = "stt"

    @abc.abstractmethod
    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        """Transcribe float32 mono audio (numpy array) or 16-bit PCM bytes."""

    async def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def warmup(self) -> None:
        return None


class FasterWhisperSTT(STTEngine):
    name = "faster-whisper"

    def __init__(self, model: str = "base.en", compute_type: str = "int8", language: str = "en"):
        self.model_name = model
        self.compute_type = compute_type
        self.language = language
        self._model = None
        self._lock = asyncio.Lock()

    async def available(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper isn't installed (pip install faster-whisper)"
        return True, "ok"

    def _load(self):
        from faster_whisper import WhisperModel

        log.info("loading whisper model %s (%s)", self.model_name, self.compute_type)
        return WhisperModel(self.model_name, device="auto", compute_type=self.compute_type)

    async def warmup(self) -> None:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)

    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        await self.warmup()
        if self._model is None:
            return ""
        samples = _as_float32(audio)
        if samples is None or len(samples) < sample_rate * 0.25:
            return ""

        def run() -> str:
            segments, _info = self._model.transcribe(
                samples,
                language=self.language or None,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()

        return await asyncio.to_thread(run)


class WhisperCppSTT(STTEngine):
    name = "whispercpp"

    def __init__(self, binary: str = "whisper-cli", model_path: str = "", language: str = "en"):
        self.binary = binary
        self.model_path = model_path
        self.language = language

    async def available(self) -> tuple[bool, str]:
        import shutil

        if shutil.which(self.binary) is None:
            return False, f"{self.binary} isn't on PATH"
        if not self.model_path or not Path(self.model_path).exists():
            return False, "whispercpp_model_path isn't set to a GGML model"
        return True, "ok"

    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        samples = _as_float32(audio)
        if samples is None:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        try:
            _write_wav(path, samples, sample_rate)
            proc = await asyncio.create_subprocess_exec(
                self.binary, "-m", self.model_path, "-f", str(path), "-nt", "-l", self.language,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return out.decode("utf-8", "replace").strip()
        finally:
            path.unlink(missing_ok=True)


class NullSTT(STTEngine):
    name = "off"

    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        return ""

    async def available(self) -> tuple[bool, str]:
        return False, "speech recognition is disabled"


def build_stt(config: Any) -> STTEngine:
    voice = config.voice
    if voice.stt_engine == "faster-whisper":
        return FasterWhisperSTT(voice.stt_model, voice.stt_compute_type, voice.stt_language)
    if voice.stt_engine == "whispercpp":
        return WhisperCppSTT(voice.whispercpp_binary, voice.whispercpp_model_path,
                             voice.stt_language)
    return NullSTT()


def _as_float32(audio: Any):
    """Accept numpy float32, numpy int16, or raw 16-bit PCM bytes."""
    try:
        import numpy as np
    except ImportError:
        return None
    if isinstance(audio, (bytes, bytearray, memoryview)):
        array = np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
        return array
    if hasattr(audio, "dtype"):
        import numpy as np

        if audio.dtype == np.int16:
            return audio.astype(np.float32) / 32768.0
        return audio.astype(np.float32).flatten()
    return None


def _write_wav(path: Path, samples, sample_rate: int) -> None:
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
