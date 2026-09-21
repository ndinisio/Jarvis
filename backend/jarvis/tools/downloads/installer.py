"""Running a downloaded installer.

Deliberately its own tool, not a use of ``run_shell_command``: that general
escape hatch would only ever be able to show the user a raw shell string
("Run: installer -pkg /path -target /") for a confirmation, where this tool
shows something a person actually recognises ("Run the installer at
<path>? This changes your system and may ask for your password."), via
``ToolSpec.confirmation_template``.

``always_confirm_individually=True`` is what actually matters here: it
makes :func:`jarvis.security.consequence.classify` return ``True``
unconditionally for this tool, which means :class:`~jarvis.security.permissions.PermissionBroker`
never lets a task-scoped or remembered grant cover it (see
``registry.py``'s uniform gate) — every run is confirmed on its own, every
time, regardless of what a surrounding automation task was approved to do.
That property falls out of the ordinary risk-gating machinery; nothing in
this file has to special-case it.

Never runs as ``sudo`` with a stored credential — macOS's own ``installer``
and GUI installer flow already prompt for admin authentication themselves
when a package needs it, and holding credentials for that is not a business
JARVIS is in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.errors import SandboxViolation
from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec

_PKG_SUFFIXES = {".pkg", ".mpkg"}
_DMG_SUFFIXES = {".dmg"}


class RunInstallerTool(Tool):
    spec = ToolSpec(
        name="run_installer",
        description="Run a downloaded macOS installer (.pkg/.mpkg) or mount and open a disk image (.dmg)",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        risk=RiskLevel.HIGH,
        category="files",
        requires_macos=True,
        mutates=True,
        retryable=False,
        always_confirm_individually=True,
        confirmation_template=(
            "Run the installer at {path}? This changes your system and may ask for your password."
        ),
        expected_ms=20000,
        examples=["run the installer I just downloaded", "install that .pkg"],
    )

    def __init__(self, deps):
        self._deps = deps

    @property
    def sandbox(self):
        return self._deps.sandbox

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_path = (args.get("path") or "").strip()
        if not raw_path:
            return ToolResult.failure("I need a path to an installer.")
        try:
            path = self.sandbox.resolve(raw_path)
            self.sandbox.classify(path)  # raises SandboxViolation for a forbidden location
        except SandboxViolation as exc:
            return ToolResult.failure(exc.user_message, detail=exc.detail)
        if not path.exists():
            return ToolResult.failure(f"I can't find a file at {path}.")

        suffix = path.suffix.lower()
        if suffix in _PKG_SUFFIXES:
            return await self._run_pkg(path)
        if suffix in _DMG_SUFFIXES:
            return await self._run_dmg(path)
        return ToolResult.failure(
            f"{path.name} isn't a .pkg, .mpkg or .dmg — I don't know how to run it.",
            detail=f"unsupported installer suffix: {suffix}",
        )

    # -- .pkg / .mpkg ---------------------------------------------------------
    async def _run_pkg(self, path: Path) -> ToolResult:
        result = await self._deps.controller.run(
            ["/usr/sbin/installer", "-pkg", str(path), "-target", "/"], timeout=600.0
        )
        if not result.ok:
            return ToolResult.failure(
                f"The installer for {path.name} didn't finish successfully.",
                detail=result.output[:1000],
            )
        return ToolResult(
            data={"path": str(path), "kind": "pkg"},
            summary=f"Ran the {path.name} installer.",
        )

    # -- .dmg -------------------------------------------------------------------
    async def _run_dmg(self, path: Path) -> ToolResult:
        attach = await self._deps.controller.run(
            ["/usr/bin/hdiutil", "attach", str(path), "-nobrowse"], timeout=120.0
        )
        if not attach.ok:
            return ToolResult.failure(
                f"I couldn't mount {path.name}.", detail=attach.output[:1000]
            )
        mount_point = _mount_point(attach.stdout)
        if not mount_point:
            return ToolResult.failure(
                f"{path.name} mounted, but I couldn't work out where.",
                detail=attach.stdout[:1000],
            )

        mounted = Path(mount_point)
        packages = sorted(mounted.glob("*.pkg")) + sorted(mounted.glob("*.mpkg"))
        if packages:
            outcome = await self._run_pkg(packages[0])
            await self._deps.controller.run(["/usr/bin/hdiutil", "detach", mount_point,
                                             "-quiet"], timeout=60.0)
            return outcome

        # No .pkg inside — most likely a drag-to-Applications app bundle.
        # Trying to UI-script an arbitrary third-party installer window is
        # exactly the ungrounded-click risk the rest of this feature is
        # built to avoid, so this stops here: open it and leave it mounted
        # for the user, rather than detaching from under them.
        await self._deps.controller.open_url(mount_point)
        return ToolResult(
            data={"path": str(path), "kind": "dmg", "mount_point": mount_point},
            summary=(
                f"{path.name} is mounted and open at {mounted.name} — it doesn't contain a "
                "plain installer package, so I've stopped there for you to finish."
            ),
        )


def _mount_point(hdiutil_output: str) -> str:
    """``hdiutil attach`` prints one tab-separated line per partition; the
    data partition's line ends with its mount path (conventionally under
    ``/Volumes/``, though nothing here assumes that literal prefix — any
    absolute path in the last field is treated as the mount point)."""
    for line in reversed(hdiutil_output.strip().splitlines()):
        fields = [f.strip() for f in line.split("\t") if f.strip()]
        if fields and fields[-1].startswith("/"):
            return fields[-1]
    return ""


def installer_tools(deps) -> list[Tool]:
    return [RunInstallerTool(deps)]
