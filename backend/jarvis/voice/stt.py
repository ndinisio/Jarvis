"""Speech to text.

Local Whisper, three ways. On Apple Silicon the best is MLX Whisper — the
large-v3-turbo model on the GPU, accurate on casual speech and faster than
real time. faster-whisper (CTranslate2, CPU) is the portable default, and
whisper.cpp is supported for users who already have it — on Metal by
default, or on the Neural Engine if a Core ML encoder sits next to the
model (whisper.cpp's own convention; JARVIS only detects and reports it,
see WhisperCppSTT.coreml_active). Everything is optional: with no STT
installed, JARVIS still works by text and can fall back to the browser's
own recogniser.

Every engine can be given a *vocabulary* — installed app names, command
words, the user's own additions — passed to Whisper as its initial prompt,
which biases spelling towards words it would otherwise mishear ("Spotify",
not "spot if I"; "basket", not "bass kit").
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


#: Words people say to JARVIS that a general-purpose recogniser tends to get
#: wrong. The installed app names are added at start-up.
COMMAND_WORDS = (
    "JARVIS", "Amazon", "basket", "checkout", "Safari", "Chrome", "Spotify", "YouTube",
    "Finder", "FaceTime", "iMessage", "WhatsApp", "Wi-Fi", "Bluetooth", "screenshot",
    "Downloads", "Desktop", "tab", "inbox",
)


def vocabulary_prompt(words: list[str] | tuple[str, ...], limit: int = 120) -> str:
    """Whisper's initial prompt is conditioning text, not a list — a short
    comma-separated run of the words reads as plausible prior speech."""
    seen: list[str] = []
    for word in words:
        word = (word or "").strip()
        if word and word.lower() not in {w.lower() for w in seen}:
            seen.append(word)
        if len(seen) >= limit:
            break
    return ", ".join(seen) + "." if seen else ""


class STTEngine(abc.ABC):
    name = "stt"
    #: Conditioning text biasing recognition towards known words.
    vocabulary: str = ""

    @abc.abstractmethod
    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        """Transcribe float32 mono audio (numpy array) or 16-bit PCM bytes."""

    def set_vocabulary(self, words: list[str] | tuple[str, ...]) -> None:
        self.vocabulary = vocabulary_prompt(list(COMMAND_WORDS) + list(words))

    async def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def warmup(self) -> None:
        return None


class FasterWhisperSTT(STTEngine):
    name = "faster-whisper"

    def __init__(self, model: str = "small.en", compute_type: str = "int8", language: str = "en",
                 beam_size: int = 1):
        self.model_name = model or "small.en"
        self.compute_type = compute_type
        self.language = language
        self.beam_size = max(1, int(beam_size))
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
                beam_size=self.beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=self.vocabulary or None,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()

        return await asyncio.to_thread(run)


def _coreml_encoder_path(model_path: str) -> Path | None:
    """Where whisper.cpp looks for a Core ML encoder next to a ggml model —
    its own naming convention (a compiled ``.mlmodelc`` bundle, sibling to
    the ``.bin``), not anything JARVIS invents. ``whisper-cli`` auto-detects
    and uses it with no flag needed; this exists purely so JARVIS can tell
    the user whether theirs actually will (see README for how to build one:
    it needs whisper.cpp's own ``generate-coreml-model.sh``)."""
    path = Path(model_path)
    if not path.name.endswith(".bin"):
        return None
    return path.with_name(f"{path.name[:-len('.bin')]}-encoder.mlmodelc")


class WhisperCppSTT(STTEngine):
    name = "whispercpp"

    def __init__(self, binary: str = "whisper-cli", model_path: str = "", language: str = "en"):
        self.binary = binary
        self.model_path = model_path
        self.language = language

    @property
    def coreml_active(self) -> bool:
        """Whether transcription will run on the Neural Engine rather than
        Metal — real, not assumed: it's true only when a Core ML encoder
        actually sits next to the configured model."""
        encoder = _coreml_encoder_path(self.model_path)
        return encoder is not None and encoder.exists()

    async def available(self) -> tuple[bool, str]:
        import shutil

        if shutil.which(self.binary) is None:
            return False, f"{self.binary} isn't on PATH"
        if not self.model_path or not Path(self.model_path).exists():
            return False, "whispercpp_model_path isn't set to a GGML model"
        if self.coreml_active:
            return True, "ok, using the Core ML encoder (Neural Engine)"
        return True, "ok, Metal only — no Core ML encoder found next to the model (see README)"

    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        samples = _as_float32(audio)
        if samples is None:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        try:
            _write_wav(path, samples, sample_rate)
            argv = [self.binary, "-m", self.model_path, "-f", str(path), "-nt", "-l", self.language]
            if self.vocabulary:
                argv += ["--prompt", self.vocabulary]
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return out.decode("utf-8", "replace").strip()
        finally:
            path.unlink(missing_ok=True)


class MLXWhisperSTT(STTEngine):
    """Whisper on the Apple Silicon GPU via MLX (``pip install mlx-whisper``)."""

    name = "mlx"
    DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

    def __init__(self, model: str = "", language: str = "en"):
        self.model_name = model or self.DEFAULT_MODEL
        self.language = language
        self._lock = asyncio.Lock()
        self._warm = False

    @staticmethod
    def installed() -> bool:
        import importlib.util
        import platform

        return (platform.system() == "Darwin" and platform.machine() == "arm64"
                and importlib.util.find_spec("mlx_whisper") is not None)

    async def available(self) -> tuple[bool, str]:
        if not self.installed():
            return False, "mlx-whisper isn't installed (pip install mlx-whisper; Apple Silicon only)"
        return True, "ok"

    def _run(self, samples) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            samples, path_or_hf_repo=self.model_name, language=self.language or None,
            initial_prompt=self.vocabulary or None, condition_on_previous_text=False,
        )
        return str(result.get("text") or "").strip()

    async def warmup(self) -> None:
        async with self._lock:
            if self._warm or not self.installed():
                return
            try:
                import numpy as np

                # The first call downloads (once) and loads the weights.
                await asyncio.to_thread(self._run, np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
                self._warm = True
            except Exception as exc:  # pragma: no cover - platform dependent
                log.warning("mlx-whisper warm-up failed: %s", exc)

    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        samples = _as_float32(audio)
        if samples is None or len(samples) < sample_rate * 0.25:
            return ""
        await self.warmup()
        return await asyncio.to_thread(self._run, samples)


class NullSTT(STTEngine):
    name = "off"

    async def transcribe(self, audio: Any, sample_rate: int = SAMPLE_RATE) -> str:
        return ""

    async def available(self) -> tuple[bool, str]:
        return False, "speech recognition is disabled"


def build_stt(config: Any) -> STTEngine:
    voice = config.voice
    engine = voice.stt_engine
    if engine == "auto":
        engine = "mlx" if MLXWhisperSTT.installed() else "faster-whisper"
    if engine == "mlx":
        stt: STTEngine = MLXWhisperSTT(voice.stt_model, voice.stt_language)
    elif engine == "faster-whisper":
        stt = FasterWhisperSTT(voice.stt_model, voice.stt_compute_type, voice.stt_language,
                               voice.stt_beam_size)
    elif engine == "whispercpp":
        stt = WhisperCppSTT(voice.whispercpp_binary, voice.whispercpp_model_path, voice.stt_language)
    else:
        return NullSTT()
    stt.set_vocabulary(list(voice.stt_vocabulary))
    return stt


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
