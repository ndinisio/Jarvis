"""Text to speech.

macOS' own `say` is the default: it is offline, instant to start, and the
British system voices (Daniel, Serena, Oliver) suit the character. The engine
sits behind an interface so a neural voice can be swapped in later without
touching anything else — which is exactly what :class:`KokoroTTS` is: an
optional, higher-fidelity local voice for anyone willing to download its
model files.

Speech is a *queue* with barge-in: :meth:`stop` kills the current utterance and
drops the rest, which is what makes interruption feel immediate.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import platform
import shutil
from pathlib import Path
from typing import Any

from ..core.events import EventBus, EventType
from ..core.logging import get_logger

log = get_logger("jarvis.voice.tts")


class TTSEngine(abc.ABC):
    name = "tts"

    @abc.abstractmethod
    async def speak(self, text: str) -> bool:
        """Speak, returning when the utterance finishes (or is interrupted)."""

    @abc.abstractmethod
    async def stop(self) -> bool:
        """Interrupt the current utterance. Returns True if something was stopped."""

    async def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def voices(self) -> list[dict[str, str]]:
        return []


class MacSayTTS(TTSEngine):
    """macOS `say`. Offline, low latency, no model to load."""

    name = "macos"

    def __init__(self, voice: str = "Daniel", rate: int = 190):
        self.voice = voice
        self.rate = rate
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def available(self) -> tuple[bool, str]:
        if platform.system() != "Darwin":
            return False, "macOS speech is only available on macOS"
        if shutil.which("say") is None:
            return False, "the `say` command is missing"
        return True, "ok"

    async def speak(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        async with self._lock:
            argv = ["say"]
            if self.voice:
                argv += ["-v", self.voice]
            if self.rate:
                argv += ["-r", str(int(self.rate))]
            argv += ["--", text]
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
            except FileNotFoundError:
                log.warning("`say` is unavailable; speech disabled")
                return False
            try:
                _, err = await self._process.communicate()
            except asyncio.CancelledError:
                await self.stop()
                raise
            finally:
                code = self._process.returncode if self._process else 0
                self._process = None
            if code not in (0, -15, -9, 255):
                log.debug("say exited with %s: %s", code, err.decode("utf-8", "replace")[:200])
                return False
            return True

    async def stop(self) -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return False
        try:
            process.terminate()
        except ProcessLookupError:
            return False
        return True

    async def voices(self) -> list[dict[str, str]]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", "?", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
        except (FileNotFoundError, OSError):
            return []
        voices: list[dict[str, str]] = []
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            locale = next((p for p in parts if "_" in p and len(p) == 5), "")
            name = line.split(locale)[0].strip() if locale else parts[0]
            voices.append({"name": name, "locale": locale})
        return voices


class KokoroTTS(TTSEngine):
    """Kokoro — a small, fast, open-weight local neural voice.

    Runs via ``kokoro-onnx`` (CPU-friendly, no PyTorch) rather than the
    reference ``kokoro``/``misaki`` package, matching this codebase's existing
    choice of ``faster-whisper`` over vanilla ``whisper`` for STT: nothing
    else here needs a multi-hundred-MB PyTorch install. Synthesis happens in
    a worker thread (``kokoro-onnx`` is not async); the resulting waveform is
    played through `sounddevice` — already a `voice`-extras dependency, used
    today for microphone capture (``voice/audio.py``), so this needs no new
    playback library.

    Requires the model + voices files to be downloaded manually first (see
    README.md) — there is no silent large download here, the same rule every
    other optional model in this project follows.
    """

    name = "kokoro"
    #: Kokoro's native output sample rate.
    SAMPLE_RATE = 24000

    def __init__(self, model_path: str = "", voices_path: str = "", voice: str = "bm_lewis",
                 speed: float = 1.0, lang: str = "en-gb"):
        self.model_path = model_path
        self.voices_path = voices_path
        self.voice = voice
        self.speed = speed
        self.lang = lang
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        #: Serialises speak() the same way MacSayTTS._lock does — without it,
        #: two overlapping calls (e.g. the /api/voice/speak route firing
        #: while a streamed reply is mid-drain) would each spawn a worker
        #: thread writing the same self._stream, and stop() could see one
        #: thread's cleanup clear it while the other is still playing.
        self._speak_lock = asyncio.Lock()
        #: True for the whole span of one speak() call — synthesis and
        #: playback both — so stop() can report "yes, something was
        #: interrupted" even during synthesis, before self._stream exists.
        self._speaking = False
        #: Checked right after synthesis finishes and again immediately
        #: before opening the output stream — the closest this gets to
        #: barge-in during synthesis without a cancellation hook into
        #: kokoro-onnx's own (CPU-bound, non-async) inference call.
        self._stop_requested = False
        #: The in-flight sounddevice output stream, if any — stop() aborts it
        #: from whatever thread called stop(), which is what sounddevice's
        #: stream objects are meant to support.
        self._stream: Any = None

    async def available(self) -> tuple[bool, str]:
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False, 'kokoro-onnx isn\'t installed (pip install -e ".[kokoro]")'
        try:
            import sounddevice  # noqa: F401
        except ImportError:
            return False, 'sounddevice isn\'t installed (pip install -e ".[voice]")'
        if not self.model_path or not Path(self.model_path).is_file():
            return False, "kokoro_model_path isn't set to a downloaded .onnx model"
        if not self.voices_path or not Path(self.voices_path).is_file():
            return False, "kokoro_voices_path isn't set to a downloaded voices file"
        return True, "ok"

    def _load(self):
        from kokoro_onnx import Kokoro

        log.info("loading kokoro model %s", self.model_path)
        return Kokoro(self.model_path, self.voices_path)

    async def warmup(self) -> None:
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)

    async def voices(self) -> list[dict[str, str]]:
        await self.warmup()
        if self._model is None:
            return []
        try:
            names = await asyncio.to_thread(lambda: sorted(self._model.get_voices()))
        except Exception as exc:  # pragma: no cover - defensive, depends on kokoro-onnx internals
            log.debug("kokoro voice listing failed: %s", exc)
            return []
        return [{"name": name, "locale": self.lang} for name in names]

    async def speak(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        async with self._speak_lock:
            self._speaking = True
            self._stop_requested = False
            try:
                try:
                    await self.warmup()
                except Exception as exc:
                    # A broken model file must fail this one speak() call,
                    # not propagate: uncaught, this would surface only via
                    # VoiceManager's wake-ack path straight into
                    # _listen_loop()'s outer handler, tearing down wake-word
                    # detection and STT along with it — an outsized blast
                    # radius for an isolated TTS problem.
                    log.warning("kokoro model failed to load: %s", exc)
                    return False
                if self._model is None:
                    return False
                try:
                    samples, sample_rate = await asyncio.to_thread(
                        self._model.create, text, voice=self.voice, speed=self.speed,
                        lang=self.lang,
                    )
                except Exception as exc:
                    log.warning("kokoro synthesis failed: %s", exc)
                    return False
                if self._stop_requested:
                    return False
                try:
                    await asyncio.to_thread(self._play_blocking, samples, sample_rate)
                except asyncio.CancelledError:
                    await self.stop()
                    raise
                except Exception as exc:
                    log.debug("kokoro playback ended early: %s", exc)
                    return False
                return True
            finally:
                self._speaking = False

    def _play_blocking(self, samples: Any, sample_rate: int) -> None:
        """Runs in a worker thread. ``stop()`` aborts ``self._stream`` from
        the event-loop thread while this is blocked in ``write()``."""
        import numpy as np
        import sounddevice as sd

        if self._stop_requested:
            return
        stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
        self._stream = stream
        try:
            stream.start()
            stream.write(np.asarray(samples, dtype="float32"))
        finally:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
            self._stream = None

    async def stop(self) -> bool:
        was_speaking = self._speaking
        self._stop_requested = True
        stream = self._stream
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.abort()
        return was_speaking


class BrowserTTS(TTSEngine):
    """Speech synthesised in the UI.

    Used off macOS, and as a fallback when `say` is unavailable. The backend
    emits a speech event and the frontend hands it to the Web Speech API, so the
    rest of JARVIS is unaware of the difference.
    """

    name = "browser"

    def __init__(self, bus: EventBus, voice: str = "", rate: float = 1.0):
        self._bus = bus
        self.voice = voice
        self.rate = rate
        self._speaking = False
        self._finished = asyncio.Event()

    def notify_finished(self) -> None:
        """Called when the browser reports that an utterance has ended."""
        self._finished.set()

    async def speak(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        self._speaking = True
        self._finished.clear()
        self._bus.publish(EventType.SPEECH_START, text=text, engine="browser",
                          voice=self.voice, rate=self.rate)
        # Wait for the browser to say it has finished, with a generous estimate
        # as the backstop in case the tab is closed or muted.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._finished.wait(), timeout=min(20.0, 0.09 * len(text) + 2))
        self._speaking = False
        self._bus.publish(EventType.SPEECH_END, engine="browser")
        return True

    async def stop(self) -> bool:
        if not self._speaking:
            return False
        self._speaking = False
        self._finished.set()
        self._bus.publish(EventType.SPEECH_END, engine="browser", interrupted=True)
        return True


class NullTTS(TTSEngine):
    name = "off"

    async def speak(self, text: str) -> bool:
        return False

    async def stop(self) -> bool:
        return False

    async def available(self) -> tuple[bool, str]:
        return False, "speech output is disabled"


def build_tts(config: Any, bus: EventBus) -> TTSEngine:
    voice_config = config.voice
    engine = voice_config.tts_engine
    if engine == "macos" and platform.system() == "Darwin":
        return MacSayTTS(voice_config.tts_voice, voice_config.tts_rate)
    if engine == "kokoro":
        return KokoroTTS(voice_config.kokoro_model_path, voice_config.kokoro_voices_path,
                         voice_config.tts_voice)
    if engine in {"macos", "browser"}:
        return BrowserTTS(bus, voice_config.tts_voice, max(0.5, voice_config.tts_rate / 190.0))
    return NullTTS()
