"""Configuration.

Configuration lives in three layers, highest priority first:

1. environment variables (``JARVIS_*``, also read from a ``.env`` file)
2. the user's config file (``~/JARVIS/config/config.json``)
3. the defaults in this module

Nothing in the application is allowed to hard-code a model name, a path, a
voice or a timeout: everything routes through :class:`Config`.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_ENV_PREFIX = "JARVIS_"


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


class ModelSlotConfig(BaseModel):
    """One routing slot (fast / general / reasoning / vision / specialist)."""

    provider: str = "ollama"
    model: str = ""
    temperature: float = 0.4
    max_tokens: int = 700
    timeout_s: float = 60.0
    #: Context window in tokens (Ollama's ``num_ctx``). 0 leaves the server's
    #: own default, which is small enough to silently cut the front off an
    #: agent prompt that carries a page's elements.
    num_ctx: int = 0
    #: How long Ollama keeps the model loaded after a call.
    keep_alive: str = "30m"


class ProviderConfig(BaseModel):
    kind: Literal["ollama", "openai", "anthropic"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    enabled: bool = True


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "ollama": ProviderConfig(kind="ollama", base_url="http://127.0.0.1:11434"),
            "openai": ProviderConfig(kind="openai", base_url="https://api.openai.com/v1", enabled=False),
            "anthropic": ProviderConfig(
                kind="anthropic", base_url="https://api.anthropic.com", enabled=False
            ),
        }
    )
    #: Small, very fast model: routing, classification, greetings, short rewrites.
    fast: ModelSlotConfig = ModelSlotConfig(model="llama3.2:1b", max_tokens=200, timeout_s=20.0,
                                            num_ctx=4096)
    #: Capable model: conversation, reasoning, synthesis.
    general: ModelSlotConfig = ModelSlotConfig(model="llama3.1:8b", max_tokens=900, timeout_s=120.0,
                                               num_ctx=8192)
    #: Vision model: screen understanding.
    vision: ModelSlotConfig = ModelSlotConfig(model="llava:7b", max_tokens=600, timeout_s=180.0)
    #: Cheap, frequent captures for the background screen watcher (see
    #: ScreenAwarenessConfig). Left empty it defers to ``vision``, so nothing
    #: has to be installed for it to work; set a small/fast model here (e.g.
    #: ``moondream``) to keep the watcher's frequent calls cheap without
    #: touching on-demand `analyse_screen` quality.
    screen_watch: ModelSlotConfig = ModelSlotConfig(model="", max_tokens=300, timeout_s=30.0)
    #: Deliberate model: understanding, planning, tool choice, verification and
    #: repair. Left empty it defers to ``general``, so JARVIS is intelligent out
    #: of the box; set a model here to give the agentic loop a stronger brain
    #: than conversation needs. A longer timeout is deliberate — this slot is
    #: asked for structured output, which is worth waiting a little longer for.
    reasoning: ModelSlotConfig = ModelSlotConfig(
        model="", temperature=0.1, max_tokens=700, timeout_s=90.0, num_ctx=8192
    )
    #: Optional domain model (code, maths, a local fine-tune). Empty defers to
    #: ``reasoning``; nothing has to be installed for this slot to be asked for.
    specialist: ModelSlotConfig = ModelSlotConfig(
        model="", temperature=0.2, max_tokens=900, timeout_s=120.0
    )
    #: Candidate models tried, in order, when a slot's model isn't installed.
    fallbacks: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "fast": ["llama3.2:1b", "qwen2.5:1.5b", "gemma3:1b", "qwen2.5:3b", "phi3:mini", "llama3.2:3b"],
            "general": [
                "llama3.1:8b",
                "qwen2.5:7b",
                "gemma3:4b",
                "mistral:7b",
                "llama3.2:3b",
                "qwen2.5:3b",
            ],
            "vision": ["llava:7b", "qwen2.5vl:7b", "llama3.2-vision:11b", "moondream", "minicpm-v"],
            # Only consulted once reasoning/specialist have a model of their
            # own; an empty slot defers to another slot instead.
            "reasoning": [
                "qwen2.5:14b",
                "llama3.1:8b",
                "qwen2.5:7b",
                "mistral:7b",
                "llama3.2:3b",
            ],
            "specialist": [],
            "screen_watch": [],
        }
    )


class VoiceConfig(BaseModel):
    enabled: bool = True
    wake_word: str = "jarvis"
    #: "openwakeword" (offline neural detector) | "whisper" (chunked keyword spotting) | "off"
    wake_engine: Literal["openwakeword", "whisper", "off"] = "openwakeword"
    wake_sensitivity: float = 0.5
    #: How long JARVIS keeps listening for a follow-up after answering.
    conversation_window_s: float = 12.0
    #: Speech-to-text: "faster-whisper" (local) | "whispercpp" | "off"
    stt_engine: Literal["faster-whisper", "whispercpp", "off"] = "faster-whisper"
    stt_model: str = "base.en"
    stt_compute_type: str = "int8"
    stt_language: str = "en"
    whispercpp_binary: str = "whisper-cli"
    whispercpp_model_path: str = ""
    #: Text-to-speech: "macos" (the `say` command) | "kokoro" (local neural
    #: voice, needs kokoro_model_path/kokoro_voices_path — see README.md) |
    #: "browser" | "off"
    tts_engine: Literal["macos", "kokoro", "browser", "off"] = "macos"
    #: Free text, not validated: a macOS voice name ("Daniel") for the
    #: "macos" engine, or a Kokoro voice code ("bm_lewis") for "kokoro".
    tts_voice: str = "Daniel"
    tts_rate: int = 190
    #: Path to Kokoro's downloaded .onnx model file. Required for tts_engine
    #: "kokoro"; there's no auto-download.
    kokoro_model_path: str = ""
    #: Path to Kokoro's downloaded voices file (paired with the model above).
    kokoro_voices_path: str = ""
    #: Duck/stop speech as soon as the user starts talking.
    barge_in: bool = True
    input_device: str = ""
    silence_threshold: float = 0.012
    max_utterance_s: float = 20.0
    silence_tail_s: float = 0.9


class PersonalityConfig(BaseModel):
    name: str = "JARVIS"
    address_user_as: str = "sir"
    #: 0.0 = terse machine, 1.0 = chatty. 0.35 is the intended house style.
    warmth: float = 0.35
    #: How often "sir" is allowed to appear (probabilistic, not every sentence).
    honorific_frequency: float = 0.35
    style_notes: str = (
        "British, composed, precise, subtly witty, never theatrical. "
        "Prefers one good sentence to three mediocre ones."
    )


class SecurityConfig(BaseModel):
    #: How much JARVIS asks while carrying out something you asked it to do.
    #:
    #: ``consequential_only`` — routine steps (opening, clicking, typing,
    #: filling in a form, adding to a basket) just happen. Anything
    #: consequential — paying or placing an order, sending, deleting,
    #: installing, typing into a terminal — always asks first.
    #: ``confirm_start`` — as above, but a multi-step task's plan is also
    #: confirmed once before it starts, and routine steps outside a task ask.
    #: ``confirm_each_step`` — every step that changes anything asks.
    #:
    #: HIGH-risk actions and privacy consents ask whatever this says.
    autonomy: Literal["consequential_only", "confirm_start", "confirm_each_step"] = "consequential_only"
    #: Risk levels that execute without asking.
    auto_approve: list[str] = Field(default_factory=lambda: ["low"])
    #: Risk levels that always require an explicit confirmation.
    always_confirm: list[str] = Field(default_factory=lambda: ["high"])
    confirmation_timeout_s: float = 90.0
    #: Directories readable outside the workspace (medium risk, still sandboxed).
    readable_roots: list[str] = Field(
        default_factory=lambda: ["~/Documents", "~/Downloads", "~/Desktop"]
    )
    allow_shell: bool = True
    #: Commands the shell tool may run without confirmation.
    shell_allowlist: list[str] = Field(
        default_factory=lambda: [
            "ls", "cat", "head", "tail", "wc", "df", "du", "uptime", "date", "whoami",
            "sw_vers", "system_profiler", "sysctl", "vm_stat", "pmset", "ioreg", "networksetup",
            "scutil", "ps", "top", "ifconfig", "ping", "sysdiagnose", "pgrep", "which", "echo",
            "uname", "hostname", "free", "lscpu",
        ]
    )
    shell_denylist: list[str] = Field(
        default_factory=lambda: [
            "rm", "rmdir", "mkfs", "dd", "shutdown", "reboot", "halt", "kill", "killall",
            "chown", "chmod", "sudo", "su", "diskutil", "csrutil", "nvram", "launchctl",
            "curl", "wget", "ssh", "scp", "defaults", "pkill", "installer", "brew",
        ]
    )
    #: Binaries whose *version flag only* (e.g. ``python3 --version``) is a
    #: pure read with no side effect — narrowly allowed without confirmation
    #: even though the binary itself isn't on ``shell_allowlist``. Running
    #: these binaries any other way still requires the normal HIGH-risk
    #: confirmation.
    version_check_binaries: list[str] = Field(
        default_factory=lambda: ["python3", "python", "node", "npm", "git", "ruby", "java", "go"]
    )
    #: Never transmit clipboard contents to a remote provider without asking.
    clipboard_remote_guard: bool = True
    #: Screen capture is on demand by default. A separate, off-by-default
    #: ``capabilities.screen_awareness`` setting enables a throttled
    #: background watcher (see ScreenAwarenessConfig) — a cheap constant poll
    #: gating occasional, cooldown-limited vision-model calls, never literal
    #: continuous inference.
    allow_screen_capture: bool = True


class CapabilitiesConfig(BaseModel):
    email: bool = True
    calendar: bool = True
    research: bool = True
    screen: bool = True
    diagnostics: bool = True
    files: bool = True
    browser: bool = True
    clipboard: bool = True
    #: Multi-step app/web operation — page interaction, downloads, installers,
    #: and the automation capability itself. Off turns all of it off at once.
    automation: bool = True
    reminders: bool = True
    #: Read-only lookup — JARVIS never creates or edits a contact.
    contacts: bool = True
    messages: bool = True
    #: Off by default, unlike every flag above — the only tool in this
    #: package that runs something JARVIS cannot see the contents of (a
    #: user-authored Shortcut, which could be anything). The per-call
    #: confirmation is real, but exposing the surface at all is worth
    #: requiring an explicit, conscious opt-in first.
    homekit: bool = False
    #: Off by default, for a stronger version of the same reason as
    #: ``homekit`` above: a background watcher that polls what app/window is
    #: frontmost and occasionally looks at the screen unprompted operates
    #: continuously and invisibly over the single most sensitive surface on
    #: the machine. See ScreenAwarenessConfig for its tuning. Requires
    #: ``screen`` to also be on.
    screen_awareness: bool = False


class ResearchConfig(BaseModel):
    #: "duckduckgo" needs no API key. "brave" and "searxng" are optional upgrades.
    search_provider: Literal["duckduckgo", "brave", "searxng"] = "duckduckgo"
    brave_api_key: str = ""
    searxng_url: str = ""
    max_results: int = 6
    max_pages: int = 5
    max_page_chars: int = 12000
    per_request_timeout_s: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    )
    #: A page whose static fetch comes back this thin (often a sign it needs
    #: JavaScript to render) gets a second attempt via a real browser
    #: instead — see capabilities/research.py. Off entirely if
    #: capabilities.browser is off, or on a non-macOS host.
    js_fallback_enabled: bool = True
    thin_page_chars: int = 200
    #: The JS fallback opens a real, visible browser tab — there is no way
    #: to run page JavaScript invisibly through the existing AppleScript
    #: bridge (see tools/browser/tools.py). This bounds how many tabs one
    #: research turn can open, however many results looked thin.
    max_js_fallbacks: int = 2
    #: How long to keep polling a freshly opened tab for its JS to finish
    #: rendering before giving up and using whatever text is there.
    js_render_max_wait_s: float = 6.0
    js_render_poll_s: float = 0.6


class MemoryConfig(BaseModel):
    enabled: bool = True
    #: Conversation turns kept verbatim in the prompt context.
    context_turns: int = 6
    #: Facts retrieved per request by keyword relevance.
    max_facts_in_context: int = 6
    #: Let JARVIS propose durable facts from conversation.
    auto_extract_facts: bool = True


class IntelligenceConfig(BaseModel):
    """The agentic loop (V1.2).

    Turning this off reverts to V1.1 behaviour — one route, one capability, one
    answer — which is a useful thing to be able to do when something misbehaves
    and a useful baseline to measure against.
    """

    enabled: bool = True
    #: Hard ceiling on tool calls in a single turn. Six covers every worked
    #: example; the ceiling exists so a confused model cannot loop forever.
    max_steps: int = 6
    #: How many times the loop may try to repair one failing step before it
    #: reports the failure honestly instead of thrashing.
    recovery_budget: int = 2
    #: Slot used for understanding, planning, decisions, verification and
    #: repair. Empty slots defer (reasoning → general), so this works untouched.
    reasoning_slot: str = "reasoning"
    #: Publish the reasoning trace (decisions, not chain of thought) on the bus.
    trace: bool = True


class AutomationConfig(BaseModel):
    """Multi-step app/web operation (the automation capability).

    Separate from :class:`IntelligenceConfig` deliberately: the generic
    agent loop's ``max_steps=6`` is tuned for short info-gathering turns and
    would truncate a real multi-step errand silently. A dedicated,
    much larger budget here is what lets "find the best value X and add it
    to my basket" actually finish instead of being cut off mid-task.
    """

    #: Steps allowed within one milestone (e.g. one page of search results)
    #: before giving up on it.
    max_steps_per_milestone: int = 15
    #: Hard ceiling across the whole task, however many milestones it takes.
    max_total_steps: int = 60
    #: Findings kept for the model's own prompt context; the full trail is
    #: still preserved in the task's step log for the user regardless.
    findings_window: int = 16
    #: Speak a step's narration only if it ran (or is expected to run)
    #: longer than this — fast, routine steps stay silent.
    narration_action_threshold_s: float = 5.0
    #: Minimum gap between two spoken narration lines, so a slow step right
    #: after a milestone announcement doesn't talk over it.
    narration_min_gap_s: float = 4.0
    #: Hard cap on a single download's size.
    max_download_mb: int = 2048


class ScreenAwarenessConfig(BaseModel):
    """The background screen watcher (``capabilities.screen_awareness``).

    Deliberately two independent throttles: the cheap frontmost-app/window
    poll runs constantly at ``poll_interval_s``, but it only ever *gates* the
    real vision-model call, which is separately rate-limited by
    ``min_vision_interval_s`` — so rapid app-switching (Cmd-Tab cycling)
    can't fire a vision call per switch.
    """

    #: How often the cheap (app name, window id) signal is polled.
    poll_interval_s: float = 2.0
    #: Minimum gap between two actual vision-model calls, regardless of how
    #: often the cheap signal changes in between.
    min_vision_interval_s: float = 8.0
    #: Off by default — the watcher's core job is silently keeping
    #: ConversationState.screen fresh; this additionally speaks a short line
    #: (via the same narration machinery long-running tasks use) whenever a
    #: watch capture completes.
    narrate: bool = False
    #: Minimum gap between two spoken narration lines.
    narration_min_gap_s: float = 20.0
    #: Unused today — ScreenWatcher only ever calls ActionNarrator.phase(),
    #: never .maybe_narrate() — but ActionNarrator's conf_attr generalisation
    #: means any future .maybe_narrate() call against this config needs the
    #: field to exist. 0.0 means "always eligible to narrate" (no minimum
    #: slowness gate), which is a safe default given nothing reads it yet.
    narration_action_threshold_s: float = 0.0


class UIConfig(BaseModel):
    developer_mode: bool = False
    show_telemetry: bool = True
    theme: str = "obsidian"
    reduced_motion: bool = False


class EmailConfig(BaseModel):
    """Which mail backend the email tools drive.

    "apple" (the default) is ``AppleMailBackend`` — Mail.app via
    AppleScript, macOS only. "imap" is ``ImapMailBackend`` instead — a real
    IMAP/SMTP server, which works on any platform and covers an account
    that isn't (or can't be) in Apple Mail. Gmail and Outlook/Microsoft 365
    have both largely moved off plain password auth for IMAP/SMTP in favour
    of OAuth; an App Password (still free, still plain-password IMAP under
    the hood) covers Gmail, and this backend doesn't yet do a full OAuth
    flow for accounts that require one.

    The password is never a config field the way everything above it is —
    it comes from the ``JARVIS_EMAIL_PASSWORD`` environment variable only,
    exactly like a model provider's ``api_key`` (see
    ``ConfigStore.save()``, which strips both the same way before writing
    to disk).
    """

    provider: Literal["apple", "imap"] = "apple"
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    #: IMAP mailbox names for drafts/sent aren't standardised — Gmail's
    #: differ from most other providers' plain "Drafts"/"Sent" — so these
    #: are configurable rather than hard-coded.
    drafts_mailbox: str = "Drafts"
    sent_mailbox: str = "Sent"


class Config(BaseModel):
    workspace: str = "~/JARVIS"
    log_level: str = "INFO"
    server: ServerConfig = ServerConfig()
    models: ModelsConfig = ModelsConfig()
    voice: VoiceConfig = VoiceConfig()
    personality: PersonalityConfig = PersonalityConfig()
    security: SecurityConfig = SecurityConfig()
    capabilities: CapabilitiesConfig = CapabilitiesConfig()
    research: ResearchConfig = ResearchConfig()
    email: EmailConfig = EmailConfig()
    memory: MemoryConfig = MemoryConfig()
    intelligence: IntelligenceConfig = IntelligenceConfig()
    automation: AutomationConfig = AutomationConfig()
    screen_awareness: ScreenAwarenessConfig = ScreenAwarenessConfig()
    ui: UIConfig = UIConfig()
    #: Set once the first-run walkthrough has been completed.
    onboarding_complete: bool = False

    # -- derived paths -----------------------------------------------------
    @property
    def workspace_path(self) -> Path:
        return _expand(self.workspace)

    @property
    def config_dir(self) -> Path:
        return self.workspace_path / "config"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def memory_dir(self) -> Path:
        return self.workspace_path / "memory"

    @property
    def logs_dir(self) -> Path:
        return self.workspace_path / "logs"

    @property
    def tasks_dir(self) -> Path:
        return self.workspace_path / "tasks"

    @property
    def notes_dir(self) -> Path:
        return self.workspace_path / "notes"

    @property
    def captures_dir(self) -> Path:
        return self.workspace_path / "captures"

    def ensure_workspace(self) -> Path:
        root = self.workspace_path
        for d in (root, self.config_dir, self.memory_dir, self.logs_dir, self.tasks_dir,
                  self.notes_dir, self.captures_dir):
            d.mkdir(parents=True, exist_ok=True)
        return root


# ---------------------------------------------------------------------------
# Loading / saving
# ---------------------------------------------------------------------------

_ENV_MAP: dict[str, tuple[str, ...]] = {
    "HOST": ("server", "host"),
    "PORT": ("server", "port"),
    "WORKSPACE": ("workspace",),
    "LOG_LEVEL": ("log_level",),
    "OLLAMA_HOST": ("models", "providers", "ollama", "base_url"),
    "FAST_MODEL": ("models", "fast", "model"),
    "GENERAL_MODEL": ("models", "general", "model"),
    "VISION_MODEL": ("models", "vision", "model"),
    "OPENAI_BASE_URL": ("models", "providers", "openai", "base_url"),
    "OPENAI_API_KEY": ("models", "providers", "openai", "api_key"),
    "ANTHROPIC_BASE_URL": ("models", "providers", "anthropic", "base_url"),
    "ANTHROPIC_API_KEY": ("models", "providers", "anthropic", "api_key"),
    "WAKE_WORD": ("voice", "wake_word"),
    "TTS_VOICE": ("voice", "tts_voice"),
    "STT_MODEL": ("voice", "stt_model"),
    "BRAVE_API_KEY": ("research", "brave_api_key"),
    "SEARXNG_URL": ("research", "searxng_url"),
    "DEVELOPER_MODE": ("ui", "developer_mode"),
    "EMAIL_IMAP_HOST": ("email", "imap_host"),
    "EMAIL_SMTP_HOST": ("email", "smtp_host"),
    "EMAIL_USERNAME": ("email", "username"),
    "EMAIL_PASSWORD": ("email", "password"),
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(data: dict, path: tuple[str, ...], value: Any) -> None:
    cursor = data
    for part in path[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[path[-1]] = value


def _coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _env_overlay() -> dict:
    overlay: dict = {}
    for suffix, path in _ENV_MAP.items():
        raw = os.environ.get(_ENV_PREFIX + suffix)
        if raw not in (None, ""):
            # api keys / urls / credentials stay strings even if they look
            # numeric or boolean-ish — a password of "123456" or "true"
            # must never be silently coerced to an int or a bool.
            value = (raw if suffix.endswith(("API_KEY", "URL", "MODEL", "HOST", "PASSWORD",
                                             "USERNAME"))
                    else _coerce(raw))
            if suffix == "PORT":
                value = int(raw)
            _set_path(overlay, path, value)
    if os.environ.get(_ENV_PREFIX + "OPENAI_API_KEY"):
        _set_path(overlay, ("models", "providers", "openai", "enabled"), True)
    if os.environ.get(_ENV_PREFIX + "ANTHROPIC_API_KEY"):
        _set_path(overlay, ("models", "providers", "anthropic", "enabled"), True)
    if os.environ.get(_ENV_PREFIX + "BRAVE_API_KEY"):
        _set_path(overlay, ("research", "search_provider"), "brave")
    return overlay


def default_config_path() -> Path:
    root = os.environ.get(_ENV_PREFIX + "WORKSPACE", "~/JARVIS")
    return _expand(root) / "config" / "config.json"


class ConfigStore:
    """Thread-safe holder for the live configuration."""

    def __init__(self, config: Config, path: Path):
        self._config = config
        self._path = path
        self._lock = threading.RLock()
        self._listeners: list[Any] = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def current(self) -> Config:
        with self._lock:
            return self._config

    def update(self, patch: dict) -> Config:
        """Deep-merge *patch* into the live config, persist, and notify listeners."""
        with self._lock:
            merged = _deep_merge(self._config.model_dump(), patch)
            self._config = Config.model_validate(merged)
            self.save()
            config = self._config
        for listener in list(self._listeners):
            # A listener must never break a configuration write.
            with contextlib.suppress(Exception):  # pragma: no cover
                listener(config)
        return config

    def on_change(self, listener) -> None:
        self._listeners.append(listener)

    def save(self) -> None:
        with self._lock:
            data = self._config.model_dump()
            # Secrets stay in the environment; never persist them to disk.
            for provider in data.get("models", {}).get("providers", {}).values():
                if provider.get("api_key"):
                    provider["api_key"] = ""
            data.get("research", {})["brave_api_key"] = ""
            data.get("email", {})["password"] = ""
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self._path)


def load_config(path: Path | None = None) -> Config:
    """Build the effective configuration: defaults ← file ← environment."""
    data = Config().model_dump()
    path = path or default_config_path()
    if path.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            data = _deep_merge(data, json.loads(path.read_text(encoding="utf-8")))
    data = _deep_merge(data, _env_overlay())
    config = Config.model_validate(data)
    # `say` only exists on macOS; fall back to browser speech synthesis so the
    # assistant remains usable (and testable) elsewhere.
    if platform.system() != "Darwin" and config.voice.tts_engine == "macos":
        config.voice.tts_engine = "browser"
    return config


def create_store(path: Path | None = None) -> ConfigStore:
    path = path or default_config_path()
    config = load_config(path)
    config.ensure_workspace()
    store = ConfigStore(config, path)
    if not path.exists():
        store.save()
    return store
