"""Run real JARVIS turns against the mock sites and check what actually happened.

A :class:`Harness` owns the mock server and the evaluation browser for a
whole run, and builds a fresh :class:`~jarvis.core.app.JarvisApp` for every
task, so no conversation state leaks between tasks. Two model backends:

* ``oracle`` — the deterministic stand-in in ``oracle.py`` (architecture
  ceiling; runs anywhere, used by the test suite).
* ``real`` — whatever models the user's own configuration and environment
  select (Ollama, a free cloud provider…). This is the number that matters.

Confirmations are answered by a simulated user: routine ones are approved;
consequential ones are approved only when the task lists them in
``approve`` — otherwise declined, which is what the safety tasks rely on.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jarvis.core.config import Config, ConfigStore, load_config
from jarvis.core.events import EventType
from jarvis.tools.macos.controller import ShellResult

from . import checks as checks_module
from .browser import driver_for, open_browser
from .mock_sites.server import MockServer
from .oracle import OracleProvider
from .suites import WebTask


@dataclass
class TaskResult:
    id: str
    category: str
    phrasing: str
    ok: bool
    failures: list[str] = field(default_factory=list)
    wall_s: float = 0.0
    tool_calls: int = 0
    model_calls: int = 0
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    route: str = ""
    final_url: str = ""
    tools: list[str] = field(default_factory=list)
    error: str = ""
    target_phase: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_overrides(config: Config, overrides: dict[str, Any]) -> Config:
    """``{"models.general.model": "qwen3:8b"}`` → a new validated config."""
    data = config.model_dump()
    for dotted, value in overrides.items():
        cursor = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return Config.model_validate(data)


@contextlib.contextmanager
def attach_browser(app, driver):
    """Point JARVIS at the evaluation browser.

    Every web action goes to *driver* through the browser hub's own
    ``pin`` — the same code path JARVIS Chrome uses for real. The system
    "open a link" call and the frontmost-app probe are the only other ways
    out to a browser, so they are pointed at it too. Restored on exit.
    """

    async def open_url(url: str, browser: str | None = None) -> ShellResult:
        ok = await driver.open(url)
        return ShellResult(0 if ok else 1, "", "" if ok else "navigation failed")

    async def frontmost_app() -> str:
        return driver.app_name

    saved = (app.controller.open_url, app.controller.frontmost_app)
    app.controller.open_url = open_url
    app.controller.frontmost_app = frontmost_app
    try:
        with app.deps.browsers.pin(driver):
            yield
    finally:
        app.controller.open_url, app.controller.frontmost_app = saved


class Harness:
    def __init__(self, *, model: str = "oracle", config_path: Path | None = None,
                 overrides: dict[str, Any] | None = None, headless: bool = True,
                 channel: str | None = None, task_timeout_s: float = 240.0,
                 log_level: str = "WARNING"):
        if model not in {"oracle", "real"}:
            raise ValueError("model must be 'oracle' or 'real'")
        self.model = model
        self.log_level = log_level
        self.config_path = config_path
        self.overrides = dict(overrides or {})
        self.headless = headless
        self.channel = channel
        self.task_timeout_s = task_timeout_s
        self.server: MockServer | None = None
        self.browser = None
        self.driver = None
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-eval-")

    async def __aenter__(self) -> Harness:
        self.server = MockServer().start()
        self.browser = await open_browser(self.server.url, headless=self.headless, channel=self.channel)
        self.driver = driver_for(self.browser)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.browser is not None:
            await self.browser.close()
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    def build_config(self, workspace: Path) -> Config:
        config = load_config(self.config_path) if self.model == "real" else Config()
        config = apply_overrides(config, self.overrides)
        config.workspace = str(workspace)
        config.log_level = self.log_level
        config.voice.enabled = False
        config.voice.tts_engine = "off"
        config.ui.developer_mode = True
        config.capabilities.automation = True
        config.capabilities.browser = True
        return config

    def build_app(self, workspace: Path):
        from jarvis.core.app import JarvisApp

        config = self.build_config(workspace)
        config.ensure_workspace()
        store = ConfigStore(config, workspace / "config" / "config.json")
        app = JarvisApp(store, enable_voice=False)
        oracle = None
        if self.model == "oracle":
            oracle = OracleProvider()
            app.models._providers = {"ollama": oracle}
            app.models._catalog.clear()
            app.models._resolved.clear()
            for slot in ("fast", "general", "vision", "reasoning"):
                getattr(app.models._config.models, slot).model = "oracle"
        return app, oracle

    async def _reset_browser(self, start_url: str) -> None:
        context = self.browser.context
        pages = [p for p in context.pages if not p.is_closed()]
        for page in pages[1:]:
            with contextlib.suppress(Exception):
                await page.close()
        page = await self.browser.ensure_page()
        with contextlib.suppress(Exception):
            await context.clear_cookies()
        if start_url:
            await self.driver.open(start_url)
        else:
            with contextlib.suppress(Exception):
                await page.goto("about:blank")

    # ------------------------------------------------------------------
    async def run_task(self, task: WebTask, phrasing: str) -> TaskResult:
        self.server.reset(task.setup)
        await self._reset_browser(task.start_url)
        workspace = Path(self._tmp.name) / f"{task.id}-{int(time.time() * 1000)}"
        app, oracle = self.build_app(workspace)
        if oracle is not None:
            oracle.brain.begin(task.oracle)
            oracle.understood = dict(task.understood)

        record = TaskResult(id=task.id, category=task.category, phrasing=phrasing, ok=False,
                            target_phase=task.target_phase)
        with attach_browser(app, self.driver):
            await drive_turn(app, phrasing, record, approve=task.approve, oracle=oracle,
                             timeout_s=self.task_timeout_s)
        with contextlib.suppress(Exception):
            record.final_url = (await self.driver.current_page()).get("url", "")
        state = self.server.state()
        record.failures = checks_module.evaluate(task.checks, state, record.as_dict())
        if record.error:
            record.failures.insert(0, record.error)
        record.ok = not record.failures
        await app.shutdown()
        return record


async def drive_turn(app, text: str, record: TaskResult, *, approve: list[str],
                     oracle: OracleProvider | None = None, timeout_s: float = 240.0) -> None:
    """Say *text* to JARVIS as a user would, answer its confirmations as the
    simulated user, and wait for any background work it started to finish.

    Fills in *record* (tools, confirmations, answer, route, timings) and never
    raises: a crash or a timeout is recorded as the run's error.
    """
    loop = asyncio.get_running_loop()

    def on_event(event) -> None:
        payload = event.payload
        if event.type == EventType.TOOL_CALL:
            record.tool_calls += 1
            record.tools.append(str(payload.get("tool")))
        elif event.type == EventType.TOOL_RESULT and oracle is not None:
            oracle.brain.on_tool_result(str(payload.get("tool")), bool(payload.get("ok")))
        elif event.type == EventType.CONFIRM_REQUEST:
            details = payload.get("details") or {}
            # A handoff ("sign in, then say done") is the user's own work; the
            # simulated user only "does" it when the task says they would.
            handoff = bool(details.get("handoff"))
            consequential = not handoff and not details.get("offer_remember", True)
            summary = str(payload.get("summary") or "")
            approved = (not consequential and not handoff) or any(
                fragment.lower() in summary.lower() for fragment in approve)
            record.confirmations.append({"summary": summary, "consequential": consequential,
                                         "handoff": handoff, "approved": approved,
                                         "action": payload.get("action")})
            loop.call_soon(app.permissions.resolve, payload.get("id"), approved)

    app.bus.add_hook(on_event)
    started = time.perf_counter()
    try:
        await asyncio.wait_for(_converse(app, text, record), timeout=timeout_s)
    except asyncio.TimeoutError:
        record.error = f"timed out after {timeout_s:.0f}s"
        await app.tasks.shutdown()
    except Exception as exc:  # the harness reports; it never crashes a run
        record.error = f"{type(exc).__name__}: {exc}"
    record.wall_s = round(time.perf_counter() - started, 2)
    summary = app.telemetry.summary()
    record.model_calls = sum(int(summary.get(name, {}).get("count", 0))
                             for name in ("model.stream", "model.chat"))


async def _converse(app, text: str, record: TaskResult) -> None:
    result = await app.ask(text)
    record.route = f"{result.decision.kind}:{result.decision.name} via {result.decision.path}"
    record.answer = result.text
    if not result.task_id:
        return
    task = app.tasks.get(result.task_id)
    while task is not None and task.status not in {"succeeded", "failed", "cancelled"}:
        await asyncio.sleep(0.05)
    runner = task._runner if task is not None else None
    if runner is not None:
        with contextlib.suppress(Exception):
            await runner
    # The background task reports its result as a new assistant message.
    messages = [e for e in app.bus.history if e.type == EventType.ASSISTANT_MESSAGE
                and e.payload.get("task_id") == result.task_id]
    if messages:
        record.answer = str(messages[-1].payload.get("text") or record.answer)
    if task is not None and task.error:
        record.error = task.error
