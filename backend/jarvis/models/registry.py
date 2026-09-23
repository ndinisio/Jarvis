"""Model routing.

Logical *slots*, each named for a job rather than a model:

``fast``        tiny model — intent classification, greetings, short rewrites
``general``     capable model — conversation and synthesis
``reasoning``   deliberate model — understanding, planning, verification, repair
``vision``      multimodal model — screen understanding
``specialist``  optional domain model — code, maths, a local fine-tune

The router resolves a slot to a concrete (provider, model) pair at call time,
substituting an installed model when the configured one is missing, so a fresh
machine with any single Ollama model still works.

``reasoning`` and ``specialist`` are normally left unconfigured, in which case
they *defer* to another slot (see :data:`SLOT_DEFERS_TO`). That is what lets
the agentic loop ask for the right kind of thinking without requiring anyone to
download five models: the roles exist in the code from day one, and upgrading
one is a single line of configuration rather than a refactor.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..core.config import Config
from ..core.errors import ModelUnavailable
from ..core.logging import get_logger
from ..core.telemetry import Telemetry
from .anthropic import AnthropicProvider
from .base import ChatMessage, Completion, ModelProvider, extract_json
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatibleProvider

log = get_logger("jarvis.models")

_VISION_HINTS = ("llava", "vision", "-vl", "moondream", "minicpm-v", "bakllava", "gemma3", "gpt-4o",
                 "claude", "pixtral", "qwen2-vl", "qwen2.5vl")
_SIZE_ORDER = ("0.5b", "1b", "1.5b", "2b", "3b", "4b", "7b", "8b", "9b", "11b", "13b", "14b",
               "27b", "32b", "70b")


class Slot:
    FAST = "fast"
    GENERAL = "general"
    REASONING = "reasoning"
    VISION = "vision"
    SPECIALIST = "specialist"
    #: Frequent, cheap background captures for the screen watcher — separate
    #: from VISION so a background poll never has to share cost/quality
    #: tradeoffs with on-demand `analyse_screen`. Defers to VISION when
    #: unconfigured, so a fresh install works unchanged.
    SCREEN_WATCH = "screen_watch"


#: Where a slot with no model of its own sends its work. Followed transitively,
#: so an unconfigured ``specialist`` lands on ``general`` via ``reasoning``.
SLOT_DEFERS_TO = {
    Slot.REASONING: Slot.GENERAL,
    Slot.SPECIALIST: Slot.REASONING,
    Slot.SCREEN_WATCH: Slot.VISION,
}


@dataclass(slots=True)
class Resolution:
    provider: ModelProvider
    model: str
    slot: str
    substituted: bool = False


class ModelRouter:
    def __init__(self, config: Config, telemetry: Telemetry | None = None):
        self._config = config
        self._telemetry = telemetry or Telemetry()
        self._providers: dict[str, ModelProvider] = {}
        self._catalog: dict[str, tuple[float, list[str]]] = {}
        self._resolved: dict[str, tuple[float, Resolution]] = {}
        self._lock = asyncio.Lock()
        self._build_providers()

    # -- construction ------------------------------------------------------
    def _build_providers(self) -> None:
        self._providers.clear()
        for key, pconf in self._config.models.providers.items():
            if not pconf.enabled:
                continue
            try:
                if pconf.kind == "ollama":
                    self._providers[key] = OllamaProvider(pconf.base_url)
                elif pconf.kind == "openai":
                    self._providers[key] = OpenAICompatibleProvider(
                        pconf.base_url, pconf.api_key, name=key
                    )
                elif pconf.kind == "anthropic":
                    self._providers[key] = AnthropicProvider(pconf.base_url, pconf.api_key)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("provider %s could not be constructed: %s", key, exc)

    def reconfigure(self, config: Config) -> None:
        self._config = config
        self._catalog.clear()
        self._resolved.clear()
        self._build_providers()

    @property
    def providers(self) -> dict[str, ModelProvider]:
        return dict(self._providers)

    def effective_slot(self, slot: str) -> str:
        """Follow an unconfigured slot to the one that will actually serve it."""
        seen: set[str] = set()
        while slot not in seen:
            seen.add(slot)
            conf = getattr(self._config.models, slot, None)
            if conf is None:
                log.debug("unknown model slot %r, using %s", slot, Slot.GENERAL)
                return Slot.GENERAL
            if conf.model.strip() or slot not in SLOT_DEFERS_TO:
                return slot
            slot = SLOT_DEFERS_TO[slot]
        return Slot.GENERAL  # pragma: no cover - only if SLOT_DEFERS_TO cycles

    def slot_config(self, slot: str):
        return getattr(self._config.models, self.effective_slot(slot))

    # -- discovery ---------------------------------------------------------
    async def installed_models(self, provider_key: str, max_age: float = 60.0) -> list[str]:
        cached = self._catalog.get(provider_key)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        provider = self._providers.get(provider_key)
        if provider is None:
            return []
        models = await provider.list_models()
        self._catalog[provider_key] = (time.time(), models)
        return models

    async def resolve(self, slot: str, max_age: float = 60.0) -> Resolution:
        slot = self.effective_slot(slot)
        cached = self._resolved.get(slot)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        async with self._lock:
            cached = self._resolved.get(slot)
            if cached and time.time() - cached[0] < max_age:
                return cached[1]
            resolution = await self._resolve_uncached(slot)
            self._resolved[slot] = (time.time(), resolution)
            return resolution

    async def _resolve_uncached(self, slot: str) -> Resolution:
        conf = self.slot_config(slot)
        provider = self._providers.get(conf.provider)
        if provider is None:
            # Fall back to any enabled provider rather than failing outright.
            for key, candidate in self._providers.items():
                if await candidate.available():
                    provider = candidate
                    log.info("slot %s: provider %s unavailable, using %s", slot, conf.provider, key)
                    break
        if provider is None:
            raise ModelUnavailable(
                "No AI provider is configured.",
                detail="enable Ollama or add an API key in the configuration",
            )

        key = getattr(provider, "name", conf.provider)
        installed = await self.installed_models(key if key in self._providers else conf.provider)
        if not installed:
            # Provider can't enumerate (or isn't running). Trust the configuration
            # and let the call itself surface a clean error.
            return Resolution(provider, conf.model, slot)

        candidates = [conf.model] + [
            m for m in self._config.models.fallbacks.get(slot, []) if m != conf.model
        ]
        for candidate in candidates:
            match = _match(candidate, installed)
            if match:
                return Resolution(provider, match, slot, substituted=match != conf.model)

        chosen = _heuristic_pick(slot, installed)
        if chosen is None:
            raise ModelUnavailable(
                f"No suitable model is installed for the {slot} slot.",
                detail=f"installed: {', '.join(installed[:8])}",
            )
        log.info("slot %s: substituting %s (configured %s missing)", slot, chosen, conf.model)
        return Resolution(provider, chosen, slot, substituted=True)

    # -- inference ---------------------------------------------------------
    async def stream(
        self,
        slot: str,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        stop: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        conf = self.slot_config(slot)
        resolution = await self.resolve(slot)
        watch = self._telemetry.mark(
            "model.stream", slot=slot, model=resolution.model, provider=resolution.provider.name
        )
        ttft: float | None = None
        chars = 0
        runtime = ({"num_ctx": conf.num_ctx, "keep_alive": conf.keep_alive}
                   if getattr(resolution.provider, "accepts_runtime_options", False) else {})
        try:
            async for delta in resolution.provider.stream_chat(
                messages,
                resolution.model,
                temperature=conf.temperature if temperature is None else temperature,
                max_tokens=conf.max_tokens if max_tokens is None else max_tokens,
                json_mode=json_mode,
                stop=stop,
                timeout_s=conf.timeout_s if timeout_s is None else timeout_s,
                **runtime,
            ):
                if ttft is None:
                    ttft = watch.elapsed_ms
                    self._telemetry.record(
                        "model.ttft", ttft, slot=slot, model=resolution.model
                    )
                chars += len(delta)
                yield delta
        except Exception:
            watch.stop(ok=False, chars=chars)
            raise
        else:
            watch.stop(chars=chars, ttft_ms=round(ttft or 0.0, 2))

    async def complete(
        self,
        slot: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> Completion:
        t0 = time.perf_counter()
        parts: list[str] = []
        async for delta in self.stream(slot, messages, **kwargs):
            parts.append(delta)
        resolution = await self.resolve(slot)
        return Completion(
            text="".join(parts).strip(),
            model=resolution.model,
            provider=resolution.provider.name,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def complete_json(self, slot: str, messages: list[ChatMessage], **kwargs) -> dict | None:
        kwargs.setdefault("json_mode", True)
        kwargs.setdefault("temperature", 0.0)
        completion = await self.complete(slot, messages, **kwargs)
        return extract_json(completion.text)

    # -- health ------------------------------------------------------------
    async def status(self) -> dict:
        providers: dict[str, dict] = {}
        for key, provider in self._providers.items():
            ok = await provider.available()
            providers[key] = {
                "kind": provider.name,
                "local": provider.local,
                "available": ok,
                "models": await self.installed_models(key) if ok else [],
                "base_url": getattr(provider, "base_url", ""),
            }
        slots: dict[str, dict] = {}
        for slot in (Slot.FAST, Slot.GENERAL, Slot.VISION):
            conf = self.slot_config(slot)
            entry: dict = {"configured": conf.model, "provider": conf.provider}
            provider_up = providers.get(conf.provider, {}).get("available", False)
            try:
                resolution = await self.resolve(slot, max_age=5.0)
                installed = providers.get(conf.provider, {}).get("models", [])
                entry.update(
                    {
                        "resolved": resolution.model,
                        "substituted": resolution.substituted,
                        # A slot is only ready if its provider answered *and* the
                        # model is actually installed there.
                        "ready": provider_up and (not installed or resolution.model in installed),
                    }
                )
                if not provider_up:
                    entry["reason"] = f"the {conf.provider} provider isn't reachable"
                elif not entry["ready"]:
                    entry["reason"] = f"{resolution.model} isn't installed"
            except ModelUnavailable as exc:
                entry.update({"resolved": None, "ready": False, "reason": exc.user_message})
            slots[slot] = entry
        return {"providers": providers, "slots": slots}

    async def warmup(self, slot: str = Slot.FAST) -> bool:
        """Load a model into memory so the first real request isn't the cold one."""
        try:
            await self.complete(
                slot,
                [ChatMessage("user", "Reply with the single word: ready")],
                max_tokens=5,
                temperature=0.0,
            )
            return True
        except Exception as exc:
            log.debug("warmup(%s) failed: %s", slot, exc)
            return False

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()


def _match(candidate: str, installed: list[str]) -> str | None:
    lowered = {m.lower(): m for m in installed}
    cand = candidate.lower()
    if cand in lowered:
        return lowered[cand]
    if ":" not in cand and f"{cand}:latest" in lowered:
        return lowered[f"{cand}:latest"]
    base = cand.split(":")[0]
    for name in installed:
        if name.lower().split(":")[0] == base:
            return name
    return None


def _size_rank(name: str) -> int:
    lowered = name.lower()
    for i, token in enumerate(_SIZE_ORDER):
        if token in lowered:
            return i
    return len(_SIZE_ORDER)


def _is_vision(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _VISION_HINTS)


def _is_embedding(name: str) -> bool:
    lowered = name.lower()
    return "embed" in lowered or "bge" in lowered or "nomic" in lowered


def _heuristic_pick(slot: str, installed: list[str]) -> str | None:
    usable = [m for m in installed if not _is_embedding(m)]
    if not usable:
        return None
    if slot in (Slot.VISION, Slot.SCREEN_WATCH):
        vision = [m for m in usable if _is_vision(m)]
        return sorted(vision, key=_size_rank)[0] if vision else None
    chat = [m for m in usable if not _is_vision(m)] or usable
    ordered = sorted(chat, key=_size_rank)
    if slot == Slot.FAST:
        return ordered[0]
    return ordered[-1] if len(ordered) > 1 else ordered[0]
