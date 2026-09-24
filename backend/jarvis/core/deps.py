"""The dependency container.

One object carries the shared infrastructure — configuration, event bus,
telemetry, permissions, models, memory, tasks, OS control — into every tool and
capability. Capabilities are therefore plain objects with no global state and
are trivial to construct in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import Config, ConfigStore
from .events import EventBus
from .telemetry import Telemetry

if TYPE_CHECKING:  # pragma: no cover
    from ..memory.store import MemoryStore
    from ..models.registry import ModelRouter
    from ..security.permissions import PermissionBroker
    from ..tasks.manager import TaskManager
    from ..tools.files.sandbox import FileSandbox
    from ..tools.macos.apps import AppCatalog
    from ..tools.macos.controller import MacOSController
    from ..tools.system.diagnostics import Diagnostics
    from ..tools.system.info import SystemInfo


@dataclass
class Deps:
    config_store: ConfigStore
    bus: EventBus
    telemetry: Telemetry
    permissions: PermissionBroker
    models: ModelRouter
    memory: MemoryStore
    tasks: TaskManager
    controller: MacOSController
    apps: AppCatalog
    sysinfo: SystemInfo
    diagnostics: Diagnostics
    sandbox: FileSandbox
    registry: Any = None  # ToolRegistry, filled in after tools are built
    voice: Any = None  # VoiceManager | None, filled in after tools are built (see core/app.py)
    #: BrowserHub — which browser a web action goes to (surfaces/web/hub.py).
    browsers: Any = None

    @property
    def config(self) -> Config:
        return self.config_store.current

    def tool_context(self, task: Any = None, **overrides) -> Any:
        """Build a :class:`ToolContext`.

        Passing a task wires up its id, cancellation token and progress
        reporting, so tools running inside a background task can be cancelled
        and can narrate what they are doing without knowing about tasks.
        """
        from ..tools.base import ToolContext

        base: dict[str, Any] = dict(
            config=self.config,
            bus=self.bus,
            telemetry=self.telemetry,
            permissions=self.permissions,
            models=self.models,
            memory=self.memory,
        )
        if task is not None:
            base.update(
                task_id=task.id,
                cancel_event=task.cancel_event,
                progress=lambda message, meta=None: self.tasks.step(
                    task, message, **(meta or {})
                ),
            )
        base.update(overrides)
        return ToolContext(**base)
