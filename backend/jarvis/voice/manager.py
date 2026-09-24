"""The voice manager.

Owns the whole spoken interface:

* the wake-word loop ("Jarvis" → "Yes, sir?")
* utterance capture and transcription
* the conversation window, so a follow-up doesn't need the wake word again
* the speech queue, with interruption and barge-in

Everything degrades: no microphone library, no wake model, no Whisper — each
missing piece disables its own feature and reports why, while text interaction
and the browser microphone fallback keep working.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

from ..core import latency
from ..core.config import Config
from ..core.events import AssistantState, EventBus, EventType
from ..core.logging import get_logger
from ..core.telemetry import Telemetry
from ..core.tracing import current_turn_id
from .audio import Microphone, record_utterance, to_int16_frame
from .stt import build_stt
from .tts import build_tts
from .wakeword import WhisperWakeDetector, build_wake_detector

log = get_logger("jarvis.voice")


#: Consecutive per-frame detector failures tolerated before the wake loop gives
#: up. A transient fault shouldn't kill listening; a permanent one shouldn't be
#: retried 12 times a second in silence.
_MAX_WAKE_FAILURES = 5


class VoiceState:
    OFF = "off"
    WAITING_FOR_WAKE = "waiting_for_wake"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    SPEAKING = "speaking"


class VoiceManager:
    def __init__(self, config: Config, bus: EventBus, telemetry: Telemetry,
                 on_utterance: Callable[[str, str], Any] | None = None):
        self._config = config
        self._bus = bus
        self._telemetry = telemetry
        self._on_utterance = on_utterance

        self.tts = build_tts(config, bus)
        self.stt = build_stt(config)
        #: Words taught at start-up (installed apps); re-applied whenever the
        #: recogniser is rebuilt by a configuration change.
        self._learned_words: list[str] = []
        self.wake = build_wake_detector(config, self.stt)
        self.microphone = Microphone(config.voice.input_device or None)

        self.state = VoiceState.OFF
        self._listen_task: asyncio.Task | None = None
        self._speech_task: asyncio.Task | None = None
        #: Sentences to speak, each with the request whose result it is
        #: (core/latency.py) — so "spoken" is timed when speech really starts.
        self._speech_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        #: How long the latest transcription took (for request timings).
        self.last_recognition_ms = 0.0
        self._conversation_until = 0.0
        self._status: dict[str, Any] = {}
        self._speaking = False
        #: Serialises start(): probe() awaits several device checks, and two
        #: overlapping calls (the automatic startup call and a user-triggered
        #: "voice.start" arriving while it's still probing, say) could both
        #: pass the "already listening" check before either had set
        #: _listen_task, each then creating its own _listen_loop — two
        #: capture/dispatch pipelines racing on the same microphone. The lock
        #: makes the check-then-create atomic instead of racy.
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    async def probe(self) -> dict[str, Any]:
        """Check every voice component and report what's usable and why."""
        mic_ok, mic_note = Microphone.available()
        tts_ok, tts_note = await self.tts.available()
        stt_ok, stt_note = await self.stt.available()
        wake_ok, wake_note = await self.wake.available()
        self._status = {
            "enabled": self._config.voice.enabled,
            "microphone": {"ok": mic_ok, "note": mic_note},
            "tts": {"ok": tts_ok, "note": tts_note, "engine": self.tts.name},
            "stt": {"ok": stt_ok, "note": stt_note, "engine": self.stt.name},
            "wake": {"ok": wake_ok, "note": wake_note, "engine": self.wake.name,
                     "word": self._config.voice.wake_word},
            "state": self.state,
            #: When local capture isn't possible the UI can still push audio or
            #: use the browser's own recogniser.
            "browser_fallback": not (mic_ok and stt_ok),
        }
        return self._status

    @property
    def status(self) -> dict[str, Any]:
        return self._status or {"state": self.state, "enabled": self._config.voice.enabled}

    def reconfigure(self, config: Config) -> None:
        was_listening = self._listen_task is not None and not self._listen_task.done()
        self._config = config
        self.tts = build_tts(config, self._bus)
        self.stt = build_stt(config)
        if self._learned_words:
            self.teach(self._learned_words)
        self.wake = build_wake_detector(config, self.stt)
        self.microphone = Microphone(config.voice.input_device or None)
        if was_listening:
            asyncio.create_task(self.restart())

    def teach(self, words: list[str]) -> None:
        """Bias recognition towards *words* (installed app names, contacts…)
        on top of the configured vocabulary."""
        self._learned_words = list(words)
        self.stt.set_vocabulary(list(self._config.voice.stt_vocabulary) + self._learned_words)

    # ------------------------------------------------------------------
    # listening
    # ------------------------------------------------------------------
    async def start(self) -> bool:
        if not self._config.voice.enabled:
            return False
        async with self._start_lock:
            # Checked first, inside the lock: a concurrent caller that
            # arrives while this one is still awaiting probe() below blocks
            # here until the first has either created _listen_task or given
            # up, then sees the up-to-date result instead of racing it.
            if self._listen_task and not self._listen_task.done():
                return True
            status = await self.probe()
            if not status["microphone"]["ok"]:
                self._emit_state(VoiceState.OFF, note=status["microphone"]["note"])
                log.info("voice input unavailable: %s", status["microphone"]["note"])
                return False
            if not status["stt"]["ok"]:
                self._emit_state(VoiceState.OFF, note=status["stt"]["note"])
                log.info("speech recognition unavailable: %s", status["stt"]["note"])
                return False
            self._listen_task = asyncio.create_task(self._listen_loop(), name="jarvis-voice")
            asyncio.create_task(self.stt.warmup())
            return True

    async def stop(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listen_task
            self._listen_task = None
        self.microphone.stop()
        self._emit_state(VoiceState.OFF)

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    async def _listen_loop(self) -> None:
        wake_available, note = await self.wake.available()
        try:
            self.microphone.start()
        except Exception as exc:
            log.warning("microphone couldn't be opened: %s", exc)
            self._bus.publish(EventType.ERROR,
                              message="Microphone access is unavailable. "
                                      "Check System Settings → Privacy & Security → Microphone.",
                              detail=str(exc))
            self._emit_state(VoiceState.OFF, note=str(exc))
            return

        if wake_available:
            try:
                await self.wake.prepare()
            except Exception as exc:
                log.exception("the wake-word model could not be loaded")
                wake_available = False
                note = str(exc)
                self._bus.publish(
                    EventType.ERROR,
                    message="The wake word couldn't be loaded, so I'll keep listening "
                            "without it. Use the microphone button to talk.",
                    detail=str(exc),
                )
        if not wake_available:
            log.info("wake word disabled: %s", note)
        self._emit_state(VoiceState.WAITING_FOR_WAKE if wake_available else VoiceState.LISTENING,
                         note=note if not wake_available else "")

        try:
            while True:
                if self._speaking:
                    await asyncio.sleep(0.05)
                    continue
                if wake_available and time.time() > self._conversation_until:
                    if not await self._await_wake():
                        continue
                    await self._acknowledge_wake()
                await self._capture_and_dispatch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice loop failed")
            self._bus.publish(EventType.ERROR, message="The voice system stopped unexpectedly.",
                              detail=str(exc))
        finally:
            self.microphone.stop()
            self._emit_state(VoiceState.OFF)

    async def _await_wake(self) -> bool:
        self._emit_state(VoiceState.WAITING_FOR_WAKE)
        watch = self._telemetry.mark("voice.wake")
        failures = 0
        async for frame in self.microphone.frames():
            if self._speaking:
                continue
            # One conversion at the boundary: every detector gets the 1-D int16
            # array openWakeWord requires, instead of raw bytes.
            try:
                samples = to_int16_frame(frame)
                score = self.wake.process(samples)
            except Exception as exc:
                # Not suppression: a detector fault is logged in full and shown
                # to the user, and a persistently broken detector stops the wake
                # loop rather than spinning on the same error forever.
                failures += 1
                log.exception("wake-word detection failed on a frame (%d/%d)",
                              failures, _MAX_WAKE_FAILURES)
                if failures == 1:
                    self._bus.publish(
                        EventType.ERROR,
                        message="Wake-word detection hit an error. I'm still listening.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                if failures >= _MAX_WAKE_FAILURES:
                    self._bus.publish(
                        EventType.ERROR,
                        message="Wake-word detection failed repeatedly and has been "
                                "stopped. Use the microphone button to talk.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    watch.stop(ok=False, engine=self.wake.name)
                    raise
                continue
            failures = 0
            if score <= 0:
                continue
            if isinstance(self.wake, WhisperWakeDetector):
                if not await self.wake.check():
                    continue
            elif score < self.wake.threshold:
                continue
            watch.stop(engine=self.wake.name)
            self._bus.publish(EventType.WAKE, word=self._config.voice.wake_word,
                              score=round(float(score), 3))
            self.wake.reset()
            return True
        return False

    async def _acknowledge_wake(self) -> None:
        """Answer the wake word instantly — no model, no network."""
        from ..core.personality import Personality

        personality = Personality(self._config)
        await self.speak(personality.wake_response())

    async def _capture_and_dispatch(self) -> None:
        self._emit_state(VoiceState.LISTENING)
        self._bus.emit_state(AssistantState.LISTENING)
        self.microphone.drain()
        voice = self._config.voice
        audio = await record_utterance(
            self.microphone,
            silence_threshold=voice.silence_threshold,
            silence_tail_s=voice.silence_tail_s,
            max_seconds=voice.max_utterance_s,
            on_level=lambda level: self._bus.publish(EventType.VOICE_STATE,
                                                     state=VoiceState.LISTENING,
                                                     level=round(level, 4)),
        )
        if audio is None:
            self._emit_state(VoiceState.WAITING_FOR_WAKE)
            self._bus.emit_state(AssistantState.IDLE)
            return

        self._emit_state(VoiceState.TRANSCRIBING)
        watch = self._telemetry.mark("voice.stt")
        text = (await self.stt.transcribe(audio)).strip()
        self.last_recognition_ms = watch.elapsed_ms
        watch.stop(chars=len(text))
        if not text:
            self._emit_state(VoiceState.WAITING_FOR_WAKE)
            self._bus.emit_state(AssistantState.IDLE)
            return

        self._conversation_until = time.time() + voice.conversation_window_s
        await self._dispatch(text, "voice")

    async def _dispatch(self, text: str, source: str) -> None:
        if self._on_utterance is None:
            self._bus.publish(EventType.TRANSCRIPT, text=text, final=True, source=source)
            return
        result = self._on_utterance(text, source)
        if asyncio.iscoroutine(result):
            await result

    # ------------------------------------------------------------------
    # push-to-talk / browser audio
    # ------------------------------------------------------------------
    async def transcribe_audio(self, data: bytes, sample_rate: int = 16000) -> str:
        """Transcribe audio captured elsewhere (the browser's microphone)."""
        watch = self._telemetry.mark("voice.stt", source="browser")
        text = (await self.stt.transcribe(data, sample_rate)).strip()
        self.last_recognition_ms = watch.elapsed_ms
        watch.stop(chars=len(text))
        return text

    # ------------------------------------------------------------------
    # speaking
    # ------------------------------------------------------------------
    async def speak(self, text: str) -> bool:
        text = (text or "").strip()
        if not text or not self._config.voice.enabled:
            return False
        self._speaking = True
        self._emit_state(VoiceState.SPEAKING)
        self._bus.emit_state(AssistantState.SPEAKING)
        self._bus.publish(EventType.SPEECH_START, text=text, engine=self.tts.name)
        # Best-effort turn_id: correct when speak() is called directly within
        # a turn (the wake acknowledgement, a quick-path reply); the queued
        # (enqueue()) case logs its own, definitely-correct turn_id at the
        # point of queueing instead, since a task drains the queue and may by
        # then be processing an item queued by a later turn.
        log.info("turn_id=%s stage=tts_start text=%r", current_turn_id(), text[:60])
        watch = self._telemetry.mark("voice.tts", chars=len(text))
        try:
            ok = await self.tts.speak(text)
        finally:
            watch.stop()
            self._speaking = False
            self._bus.publish(EventType.SPEECH_END, engine=self.tts.name)
            self._emit_state(
                VoiceState.WAITING_FOR_WAKE if self._listen_task else VoiceState.OFF
            )
        log.info("turn_id=%s stage=tts_end text=%r ok=%s", current_turn_id(), text[:60], ok)
        return ok

    def enqueue(self, text: str) -> None:
        """Queue a sentence for speech without awaiting it (streaming replies)."""
        if not text.strip() or not self._config.voice.enabled:
            return
        # Logged here, not in _drain_speech()/speak(): this call always runs
        # synchronously inside the originating turn's context, so this is
        # the one point that can log the *correct* turn_id for this specific
        # piece of text, however many turns' worth of speech end up queued
        # together.
        log.info("turn_id=%s stage=tts_enqueue text=%r", current_turn_id(), text[:60])
        self._speech_queue.put_nowait((text, latency.speech_marker()))
        if self._speech_task is None or self._speech_task.done():
            self._speech_task = asyncio.create_task(self._drain_speech())

    async def _drain_speech(self) -> None:
        while not self._speech_queue.empty():
            text, request = await self._speech_queue.get()
            if request is not None:
                self._telemetry.request_spoken(request)
            await self.speak(text)

    async def stop_speaking(self) -> bool:
        while not self._speech_queue.empty():
            try:
                self._speech_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._speech_task and not self._speech_task.done():
            self._speech_task.cancel()
        stopped = await self.tts.stop()
        if stopped:
            self._bus.publish(EventType.SPEECH_END, engine=self.tts.name, interrupted=True)
        self._speaking = False
        return stopped

    @property
    def speaking(self) -> bool:
        return self._speaking

    # ------------------------------------------------------------------
    def _emit_state(self, state: str, **payload: Any) -> None:
        self.state = state
        self._bus.publish(EventType.VOICE_STATE, state=state, engine=self.tts.name, **payload)

    def extend_conversation_window(self) -> None:
        self._conversation_until = time.time() + self._config.voice.conversation_window_s
