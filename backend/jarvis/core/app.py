"""Application assembly.

Everything is constructed here, once, and handed around as :class:`Deps`. There
are no globals and no import-time side effects, which is what makes the whole
system straightforward to test: build a :class:`JarvisApp` with a temporary
workspace and every subsystem comes with it.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from ..capabilities.registry import build_capabilities
from ..memory.store import MemoryStore
from ..models.registry import ModelRouter, Slot
from ..router.router import Router
from ..security.permissions import PermissionBroker
from ..tasks.manager import TaskManager
from ..tools.files.sandbox import FileSandbox
from ..tools.macos.apps import AppCatalog
from ..tools.macos.controller import MacOSController
from ..tools.registry import build_registry
from ..tools.system.diagnostics import Diagnostics
from ..tools.system.info import SystemInfo
from ..vision.watcher import ScreenWatcher
from ..voice.manager import VoiceManager
from .config import ConfigStore, create_store
from .deps import Deps
from .events import EventBus, EventType
from .logging import get_logger, setup_logging
from .orchestrator import Orchestrator
from .personality import Personality
from .telemetry import Telemetry

log = get_logger("jarvis.app")


class JarvisApp:
    def __init__(self, config_store: ConfigStore | None = None, *, enable_voice: bool = True):
        self.config_store = config_store or create_store()
        config = self.config_store.current
        config.ensure_workspace()
        setup_logging(config.log_level, config.logs_dir)

        self.bus = EventBus()
        self.telemetry = Telemetry(self.bus if config.ui.developer_mode else None)
        self.memory = MemoryStore(config.memory_dir / "jarvis.db")
        self.permissions = PermissionBroker(self.config_store, self.bus)
        self.models = ModelRouter(config, self.telemetry)
        self.tasks = TaskManager(self.bus, self.memory)
        self.controller = MacOSController(config.workspace_path)
        self.apps = AppCatalog(self.controller)
        self.sysinfo = SystemInfo(self.controller)
        self.diagnostics = Diagnostics(self.controller, self.sysinfo)
        self.sandbox = FileSandbox(config)

        self.deps = Deps(
            config_store=self.config_store,
            bus=self.bus,
            telemetry=self.telemetry,
            permissions=self.permissions,
            models=self.models,
            memory=self.memory,
            tasks=self.tasks,
            controller=self.controller,
            apps=self.apps,
            sysinfo=self.sysinfo,
            diagnostics=self.diagnostics,
            sandbox=self.sandbox,
        )
        self.deps.registry = build_registry(self.deps)
        self.capabilities = build_capabilities(self.deps)
        self.personality = Personality(config)
        self.router = Router(self.models, self.telemetry)

        self.voice: VoiceManager | None = None
        if enable_voice:
            self.voice = VoiceManager(config, self.bus, self.telemetry,
                                      on_utterance=self._on_voice_utterance)
        # Same after-the-fact wiring as deps.registry above: capabilities are
        # built before voice exists, but a capability that needs to narrate
        # long-running work (see core/narration.py) reaches it through here.
        self.deps.voice = self.voice

        # Off by default (capabilities.screen_awareness) — see vision/watcher.py.
        # Construction is cheap and side-effect free; start()/stop() below are
        # what actually gate on the config flag.
        self.screen_watcher = ScreenWatcher(self.deps)

        self.orchestrator = Orchestrator(
            self.deps, self.router, self.capabilities, self.personality, self.voice
        )
        self.config_store.on_change(self._on_config_change)
        self._started = False

    # ------------------------------------------------------------------
    @property
    def config(self):
        return self.config_store.current

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        log.info("JARVIS %s starting on %s", _version(), platform.platform())
        if self.voice is not None:
            await self.voice.probe()
            if self.config.voice.enabled:
                asyncio.create_task(self._teach_vocabulary())
                asyncio.create_task(self.voice.start())
        if self.config.capabilities.screen_awareness and self.config.security.allow_screen_capture:
            asyncio.create_task(self.screen_watcher.start())
        # Warm the fast model so the first real request isn't the cold one.
        asyncio.create_task(self._warmup())

    async def _teach_vocabulary(self) -> None:
        """Teach the recogniser this Mac's app names, so "open Spotify" isn't
        heard as "open spot if I"."""
        try:
            names = await self.apps.apps()
        except Exception as exc:  # pragma: no cover - platform dependent
            log.debug("couldn't list apps for the vocabulary: %s", exc)
            return
        if self.voice is not None and names:
            self.voice.teach(sorted(names, key=len)[:100])

    async def _warmup(self) -> None:
        try:
            status = await self.models.status()
            ready = [k for k, v in status["providers"].items() if v["available"]]
            if not ready:
                log.warning(
                    "no model provider is reachable — deterministic commands will still work"
                )
                self.bus.publish(
                    EventType.NOTICE,
                    level="warning",
                    message="The local AI service isn't available. Start Ollama for "
                            "conversation and reasoning; system commands work regardless.",
                )
                return
            await self.models.warmup(Slot.FAST)
            log.info("fast model ready: %s", status["slots"]["fast"].get("resolved"))
            # The first real action is decided on the reasoning slot, not the
            # fast one; loading it now is what keeps that first decision from
            # paying for a cold start.
            reasoning = self.models.effective_slot(self.config.intelligence.reasoning_slot)
            if reasoning != self.models.effective_slot(Slot.FAST):
                await self.models.warmup(reasoning)
        except Exception as exc:
            log.debug("warmup skipped: %s", exc)

    async def shutdown(self) -> None:
        if self.voice is not None:
            await self.voice.stop_speaking()
            await self.voice.stop()
        await self.screen_watcher.stop()
        await self.tasks.shutdown()
        await self.models.close()
        self.memory.close()
        log.info("JARVIS stopped")

    # ------------------------------------------------------------------
    async def ask(self, text: str, source: str = "text"):
        return await self.orchestrator.handle(text, source=source)

    def _on_voice_utterance(self, text: str, source: str):
        return self.orchestrator.handle(text, source=source)

    def _on_config_change(self, config) -> None:
        log.info("configuration updated")
        self.models.reconfigure(config)
        self.sandbox.reconfigure(config)
        self.personality.reconfigure(config)
        self.deps.registry = build_registry(self.deps)
        self.capabilities = build_capabilities(self.deps)
        self.orchestrator.capabilities = self.capabilities
        # The registry is a new object; the orchestrator's context observer and
        # the agent's tool shortlist have to follow it.
        self.orchestrator.rebind()
        config.ensure_workspace()
        if self.voice is not None:
            self.voice.reconfigure(config)
        self.screen_watcher.reconfigure(config)
        self.bus.publish(EventType.CONFIG, config=_public_config(config))

    # ------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        model_status = await self.models.status()
        voice_status = await self.voice.probe() if self.voice else {"enabled": False}
        return {
            "version": _version(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "is_macos": platform.system() == "Darwin",
            },
            "models": model_status,
            "voice": voice_status,
            "capabilities": sorted(self.capabilities),
            "tools": self.deps.registry.by_category(),
            "workspace": str(self.config.workspace_path),
            "tasks": self.tasks.snapshot()[:10],
            "config": _public_config(self.config),
            "onboarding_complete": self.config.onboarding_complete,
        }

    async def permission_report(self) -> dict[str, Any]:
        """What macOS privacy permissions are needed, and which are granted.

        Only permissions whose capability is enabled are probed — JARVIS does
        not ask for everything up front.
        """
        wanted: list[tuple[str, str, str]] = [
            ("microphone", "Voice input", "Hearing the wake word and your requests."),
        ]
        caps = self.config.capabilities
        if caps.screen:
            wanted.append(("screen_recording", "Screen Recording",
                           "Seeing your screen when you ask what's on it."))
        if caps.browser or caps.email or caps.calendar:
            wanted.append(("automation", "Automation",
                           "Controlling Safari, Mail and Calendar on your behalf."))
        wanted.append(("accessibility", "Accessibility",
                       "Reading window titles and focusing applications."))
        if caps.email:
            wanted.append(("mail", "Mail", "Reading and drafting mail."))
        if caps.calendar:
            wanted.append(("calendar", "Calendar", "Reading your schedule."))

        results = []
        for kind, label, why in wanted:
            granted, note = await self.controller.check_permission(kind)
            results.append({"kind": kind, "label": label, "why": why, "granted": granted,
                            "note": note})
        return {"permissions": results, "is_macos": platform.system() == "Darwin"}


def _public_config(config) -> dict[str, Any]:
    data = config.model_dump()
    for provider in data.get("models", {}).get("providers", {}).values():
        provider["api_key"] = "set" if provider.get("api_key") else ""
    data.get("research", {})["brave_api_key"] = (
        "set" if config.research.brave_api_key else ""
    )
    return data


def _version() -> str:
    from .. import __version__

    return __version__
