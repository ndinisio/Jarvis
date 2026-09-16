"""Text to speech.

macOS' own `say` is the default: it is offline, instant to start, and the
British system voices (Daniel, Serena, Oliver) suit the character. The engine
sits behind an interface so a neural voice can be swapped in later without
touching anything else.

Speech is a *queue* with barge-in: :meth:`stop` kills the current utterance and
drops the rest, which is what makes interruption feel immediate.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import platform
import shutil
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
    if engine in {"macos", "browser"}:
        return BrowserTTS(bus, voice_config.tts_voice, max(0.5, voice_config.tts_rate / 190.0))
    return NullTTS()
