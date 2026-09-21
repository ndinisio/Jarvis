"""Capability wiring."""

from __future__ import annotations

from .automation import AutomationCapability
from .base import Capability
from .conversation import ConversationCapability
from .diagnostics import DiagnosticsCapability
from .email import EmailCapability
from .memory_capability import MemoryCapability
from .research import ResearchCapability
from .simple import (
    AppsCapability,
    BrowserCapability,
    CalendarCapability,
    ClipboardCapability,
    FilesCapability,
    ScreenCapability,
    SystemCapability,
)


def build_capabilities(deps) -> dict[str, Capability]:
    capabilities: list[Capability] = [
        ConversationCapability(deps),
        SystemCapability(deps),
        AppsCapability(deps),
        MemoryCapability(deps),
    ]
    caps = deps.config.capabilities
    if caps.clipboard:
        capabilities.append(ClipboardCapability(deps))
    if caps.files:
        capabilities.append(FilesCapability(deps))
    if caps.screen:
        capabilities.append(ScreenCapability(deps))
    if caps.browser:
        capabilities.append(BrowserCapability(deps))
    if caps.research:
        capabilities.append(ResearchCapability(deps))
    if caps.diagnostics:
        capabilities.append(DiagnosticsCapability(deps))
    if caps.email:
        capabilities.append(EmailCapability(deps))
    if caps.calendar:
        capabilities.append(CalendarCapability(deps))
    if caps.automation:
        capabilities.append(AutomationCapability(deps))
    return {capability.name: capability for capability in capabilities}
