"""macOS control tools."""

from __future__ import annotations

from typing import Any

from ...core.errors import PermissionDenied
from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec


class OpenApplicationTool(Tool):
    spec = ToolSpec(
        name="open_application",
        description="Open or focus a macOS application by name",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Application name, e.g. Safari"}},
            "required": ["name"],
        },
        risk=RiskLevel.LOW,
        category="macos",
        expected_ms=600,
        examples=["Open Safari", "Launch Spotify", "Open System Settings"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spoken = args["name"]
        name, confidence = await self._deps.apps.resolve(spoken)
        if not name:
            suggestions = await self._deps.apps.suggestions(spoken)
            hint = f" Did you mean {suggestions[0]}?" if suggestions else ""
            # Nothing installed by that name. It may well not be an application
            # at all — "open the BBC" — so say so rather than ending the turn.
            return ToolResult.failure(f"I can't find an application called {spoken}.{hint}",
                                      wrong_tool=True)
        ctx.report(f"Opening {name}…", tool="open_application")
        result = await self._deps.controller.open_app(name)
        if not result.ok:
            detail = result.output.lower()
            if "denied" in detail or "not allowed" in detail:
                return ToolResult.failure(
                    f"{name} didn't open. macOS denied the request. "
                    "I can investigate the permission if you'd like.",
                    detail=result.output,
                )
            return ToolResult.failure(f"{name} didn't open.", detail=result.output)
        return ToolResult(
            data={"application": name, "confidence": confidence},
            summary=f"Opening {name}.",
            display={"kind": "app", "name": name},
        )


class CloseApplicationTool(Tool):
    spec = ToolSpec(
        name="close_application",
        description="Quit a running application (protected system apps are refused)",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        risk=RiskLevel.MEDIUM,
        category="macos",
        expected_ms=800,
        examples=["Close Spotify", "Quit Safari"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spoken = args["name"]
        name, _ = await self._deps.apps.resolve(spoken)
        if not name:
            return ToolResult.failure(f"I can't find an application called {spoken}.",
                                      wrong_tool=True)
        if self._deps.apps.is_protected(name):
            return ToolResult.failure(f"I'd rather not quit {name} — the system depends on it.")
        if not await self._deps.controller.is_app_running(name):
            return ToolResult(data={"application": name}, summary=f"{name} isn't running.")
        result = await self._deps.controller.quit_app(name)
        if not result.ok:
            return ToolResult.failure(f"{name} didn't respond to the quit request.",
                                      detail=result.output)
        return ToolResult(data={"application": name}, summary=f"Closed {name}.")


class ActivateApplicationTool(Tool):
    spec = ToolSpec(
        name="activate_application",
        description="Bring an already-running application to the front",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        risk=RiskLevel.LOW,
        category="macos",
        requires_macos=True,
        expected_ms=400,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spoken = args["name"]
        name, _ = await self._deps.apps.resolve(spoken)
        if not name:
            # "Focus on the medical ones" is not a request to focus a window.
            # Say so rather than sending nonsense to System Events.
            return ToolResult.failure(f"I can't find an application called {spoken}.",
                                      wrong_tool=True)
        result = await self._deps.controller.activate_app(name)
        if not result.ok:
            return ToolResult.failure(f"{name} didn't come forward.", detail=result.output)
        return ToolResult(data={"application": name}, summary=f"{name} is at the front.")


class ListApplicationsTool(Tool):
    spec = ToolSpec(
        name="list_applications",
        description="List installed or currently running applications",
        parameters={
            "type": "object",
            "properties": {
                "filter": {"type": "string", "default": ""},
                "running_only": {"type": "boolean", "default": False},
            },
        },
        risk=RiskLevel.LOW,
        category="macos",
        expected_ms=900,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if args.get("running_only"):
            apps = await self._deps.controller.running_apps()
            label = "running"
        else:
            apps = await self._deps.apps.apps()
            label = "installed"
        needle = (args.get("filter") or "").lower()
        if needle:
            apps = [a for a in apps if needle in a.lower()]
        return ToolResult(
            data={"applications": apps, "count": len(apps)},
            summary=f"{len(apps)} {label} applications.",
            display={"kind": "list", "title": f"{label.title()} applications", "items": apps[:80]},
        )


class OpenURLTool(Tool):
    spec = ToolSpec(
        name="open_url",
        description="Open a URL in the default or a named browser",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "browser": {"type": "string", "default": ""},
            },
            "required": ["url"],
        },
        risk=RiskLevel.LOW,
        category="browser",
        requires_network=True,
        expected_ms=500,
        examples=["Go to apple.com", "Open the BBC website"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = normalise_url(args["url"])
        browser = args.get("browser") or ""
        if browser:
            resolved, _ = await self._deps.apps.resolve(browser)
            browser = resolved or browser
        ctx.report(f"Opening {url}…", tool="open_url")
        result = await self._deps.controller.open_url(url, browser or None)
        if not result.ok:
            return ToolResult.failure("That page didn't open.", detail=result.output)
        return ToolResult(
            data={"url": url},
            summary=f"Opening {_domain(url)}.",
            display={"kind": "link", "url": url},
        )


class NotificationTool(Tool):
    spec = ToolSpec(
        name="send_notification",
        description="Post a macOS notification",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "default": "JARVIS"},
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
        risk=RiskLevel.LOW,
        category="macos",
        expected_ms=300,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ok = await self._deps.controller.notify(args.get("title", "JARVIS"), args["message"])
        if not ok:
            return ToolResult.failure("The notification couldn't be posted.")
        return ToolResult(summary="Notification posted.")


class VolumeTool(Tool):
    spec = ToolSpec(
        name="set_volume",
        description="Read or set the system output volume (0-100), or mute it",
        parameters={
            "type": "object",
            "properties": {
                "level": {"type": "integer"},
                "action": {"type": "string", "enum": ["set", "get", "mute", "unmute"],
                           "default": "set"},
            },
        },
        risk=RiskLevel.LOW,
        category="macos",
        expected_ms=250,
        examples=["Turn the volume down", "Mute the sound", "What's the volume?"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = args.get("action", "set")
        controller = self._deps.controller
        if action == "get" or (action == "set" and args.get("level") is None):
            level = await controller.get_volume()
            if level is None:
                return ToolResult.failure("I couldn't read the volume.")
            return ToolResult(data={"volume": level}, summary=f"Volume is at {level} percent.")
        if action in {"mute", "unmute"}:
            ok = await controller.set_muted(action == "mute")
            return (
                ToolResult(summary="Muted." if action == "mute" else "Unmuted.")
                if ok
                else ToolResult.failure("I couldn't change the mute state.")
            )
        level = max(0, min(100, int(args["level"])))
        ok = await controller.set_volume(level)
        if not ok:
            return ToolResult.failure("I couldn't change the volume.")
        return ToolResult(data={"volume": level}, summary=f"Volume set to {level} percent.")


class ShellCommandTool(Tool):
    spec = ToolSpec(
        name="run_shell_command",
        description=(
            "Run a read-only shell command. Only for diagnostics that no dedicated tool covers; "
            "destructive commands are refused"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["command"],
        },
        risk=RiskLevel.MEDIUM,
        category="system",
        expected_ms=1200,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"].strip()
        try:
            auto_ok, reason = ctx.permissions.check_shell(command)
        except PermissionDenied as exc:
            return ToolResult.failure(exc.user_message, detail=exc.detail)
        if not auto_ok:
            try:
                await ctx.permissions.require(
                    action=f"shell:{command.split()[0]}",
                    risk=RiskLevel.HIGH,
                    summary=f"Run shell command: {command}",
                    details={"command": command, "why": reason,
                             "reason": args.get("reason", "")},
                )
            except Exception as exc:
                return ToolResult.failure(getattr(exc, "user_message", "Not permitted."),
                                          detail=reason)
            result = await self._deps.controller.run_shell(command, timeout=30.0)
        else:
            result = await self._deps.controller.run(command.split(), timeout=30.0)
        output = result.output[:4000]
        return ToolResult(
            ok=result.ok,
            data={"command": command, "exit_code": result.returncode, "output": output},
            summary=output.splitlines()[0][:160] if output else ("Done." if result.ok else "No output."),
            display={"kind": "code", "title": command, "text": output},
            error=None if result.ok else result.stderr[:500],
        )


def normalise_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        return url
    if url.startswith(("http://", "https://", "file://", "mailto:", "x-apple.systempreferences:")):
        return url
    if " " in url or "." not in url:
        from urllib.parse import quote_plus

        return f"https://duckduckgo.com/?q={quote_plus(url)}"
    return "https://" + url


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


def macos_tools(deps) -> list[Tool]:
    return [
        OpenApplicationTool(deps),
        CloseApplicationTool(deps),
        ActivateApplicationTool(deps),
        ListApplicationsTool(deps),
        OpenURLTool(deps),
        NotificationTool(deps),
        VolumeTool(deps),
        ShellCommandTool(deps),
    ]
