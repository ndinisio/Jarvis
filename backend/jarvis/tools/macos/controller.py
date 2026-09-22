"""The macOS control layer.

This is the *only* place in JARVIS that shells out. Capabilities call semantic
methods (``open_app``, ``read_clipboard``, ``battery``) and never assemble
command lines themselves, which keeps the assistant off brittle UI scripting
and makes the whole surface auditable in one file.

Native mechanisms are preferred in this order: AppleScript/`osascript` →
purpose-built CLI (`pbpaste`, `screencapture`, `pmset`) → generic shell.

On a non-Darwin host every method degrades gracefully (returning a clear
"unsupported" result or a Linux equivalent) so the application remains
runnable and testable off a Mac.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...core.errors import ToolError
from ...core.logging import get_logger

log = get_logger("jarvis.macos")

IS_MACOS = platform.system() == "Darwin"


@dataclass(slots=True)
class ShellResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return self.stdout.strip() or self.stderr.strip()


class MacOSController:
    """Semantic access to the host operating system."""

    def __init__(self, workspace: Path | None = None):
        self.is_macos = IS_MACOS
        self.workspace = workspace

    # ------------------------------------------------------------------
    # process primitives
    # ------------------------------------------------------------------
    async def run(self, argv: list[str], timeout: float = 20.0,
                  stdin: str | None = None) -> ShellResult:
        """Run an argv list (never a shell string — no injection surface)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            )
        except FileNotFoundError as exc:
            return ShellResult(127, "", f"command not found: {argv[0]} ({exc})")
        except PermissionError as exc:
            return ShellResult(126, "", str(exc))
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return ShellResult(-1, "", f"timed out after {timeout}s", timed_out=True)
        return ShellResult(
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    async def run_shell(self, command: str, timeout: float = 20.0) -> ShellResult:
        """Run a command through the shell. Callers must have cleared it with
        the permission broker first."""
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return ShellResult(-1, "", f"timed out after {timeout}s", timed_out=True)
        return ShellResult(
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    async def osascript(self, script: str, language: str = "AppleScript",
                        timeout: float = 25.0) -> ShellResult:
        """Execute AppleScript (or JXA) via ``osascript``.

        The script is passed on stdin so quoting never becomes a security
        problem.
        """
        if not self.is_macos:
            return ShellResult(127, "", "AppleScript is only available on macOS")
        argv = ["osascript"]
        if language.lower() in {"javascript", "jxa"}:
            argv += ["-l", "JavaScript"]
        argv += ["-"]
        result = await self.run(argv, timeout=timeout, stdin=script)
        if not result.ok and "not allowed" in result.stderr.lower():
            log.warning("automation permission issue: %s", result.stderr.strip()[:200])
        return result

    # ------------------------------------------------------------------
    # applications
    # ------------------------------------------------------------------
    async def list_applications(self) -> list[str]:
        """Discover installed applications natively (no hard-coded list)."""
        names: set[str] = set()
        if self.is_macos:
            result = await self.run(
                ["/usr/bin/mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
                timeout=15.0,
            )
            if result.ok:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.endswith(".app"):
                        names.add(Path(line).stem)
            if not names:  # Spotlight disabled — fall back to directory scan
                for directory in ("/Applications", "/System/Applications",
                                  os.path.expanduser("~/Applications"),
                                  "/System/Applications/Utilities"):
                    try:
                        for entry in Path(directory).glob("*.app"):
                            names.add(entry.stem)
                        for entry in Path(directory).glob("*/*.app"):
                            names.add(entry.stem)
                    except OSError:
                        continue
        else:
            for directory in ("/usr/share/applications", os.path.expanduser("~/.local/share/applications")):
                try:
                    for entry in Path(directory).glob("*.desktop"):
                        names.add(entry.stem)
                except OSError:
                    continue
        return sorted(names)

    async def open_app(self, name: str) -> ShellResult:
        if self.is_macos:
            return await self.run(["/usr/bin/open", "-a", name], timeout=15.0)
        for launcher in ("gio", "xdg-open"):
            if shutil.which(launcher):
                return await self.run([launcher, name], timeout=10.0)
        return ShellResult(127, "", "no application launcher on this host")

    async def activate_app(self, name: str) -> ShellResult:
        return await self.osascript(f'tell application "{_esc(name)}" to activate')

    async def quit_app(self, name: str) -> ShellResult:
        return await self.osascript(f'tell application "{_esc(name)}" to quit')

    async def is_app_running(self, name: str) -> bool:
        if not self.is_macos:
            result = await self.run(["pgrep", "-fi", name], timeout=5.0)
            return result.ok and bool(result.stdout.strip())
        result = await self.osascript(
            'tell application "System Events" to return (exists (processes where name is '
            f'"{_esc(name)}"))'
        )
        return result.ok and result.stdout.strip().lower() == "true"

    async def frontmost_app(self) -> str:
        result = await self.osascript(
            'tell application "System Events" to return name of first application process '
            "whose frontmost is true"
        )
        return result.stdout.strip() if result.ok else ""

    async def running_apps(self) -> list[str]:
        result = await self.osascript(
            'tell application "System Events" to return name of every application process '
            "whose background only is false"
        )
        if not result.ok:
            return []
        return [n.strip() for n in result.stdout.split(",") if n.strip()]

    # ------------------------------------------------------------------
    # urls & files
    # ------------------------------------------------------------------
    async def open_url(self, url: str, browser: str | None = None) -> ShellResult:
        if self.is_macos:
            argv = ["/usr/bin/open"]
            if browser:
                argv += ["-a", browser]
            argv.append(url)
            return await self.run(argv, timeout=12.0)
        if shutil.which("xdg-open"):
            return await self.run(["xdg-open", url], timeout=10.0)
        return ShellResult(127, "", "no URL handler on this host")

    async def reveal_in_finder(self, path: str) -> ShellResult:
        if not self.is_macos:
            return ShellResult(127, "", "Finder is only available on macOS")
        return await self.run(["/usr/bin/open", "-R", path], timeout=10.0)

    # ------------------------------------------------------------------
    # clipboard
    # ------------------------------------------------------------------
    async def read_clipboard(self) -> str:
        if self.is_macos:
            result = await self.run(["/usr/bin/pbpaste"], timeout=6.0)
            return result.stdout if result.ok else ""
        for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                    ["xsel", "--clipboard", "--output"]):
            if shutil.which(cmd[0]):
                result = await self.run(cmd, timeout=6.0)
                if result.ok:
                    return result.stdout
        return _fallback_clipboard_read()

    async def write_clipboard(self, text: str) -> bool:
        if self.is_macos:
            result = await self.run(["/usr/bin/pbcopy"], timeout=6.0, stdin=text)
            return result.ok
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                result = await self.run(cmd, timeout=6.0, stdin=text)
                if result.ok:
                    return True
        _fallback_clipboard_write(text)
        return True

    async def clipboard_kind(self) -> str:
        """Best-effort description of what's on the pasteboard."""
        if not self.is_macos:
            return "text"
        result = await self.run(["/usr/bin/osascript", "-e", "clipboard info"], timeout=6.0)
        if not result.ok:
            return "unknown"
        info = result.stdout.lower()
        if "picture" in info or "tiff" in info or "png" in info:
            return "image"
        if "file url" in info or "furl" in info:
            return "file"
        return "text"

    # ------------------------------------------------------------------
    # screen
    # ------------------------------------------------------------------
    async def capture_screen(self, path: Path | None = None, *, display: int | None = None,
                             window: bool = False, interactive: bool = False) -> Path:
        """Capture the screen to a PNG. Always explicit, never continuous."""
        target = path or Path(tempfile.gettempdir()) / f"jarvis-screen-{os.getpid()}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.is_macos:
            argv = ["/usr/sbin/screencapture", "-x"]
            if interactive:
                argv.append("-i")
            elif window:
                argv += ["-o", "-l", await self.frontmost_window_id()]
            if display is not None:
                argv += ["-D", str(display)]
            argv.append(str(target))
            result = await self.run(argv, timeout=30.0)
            if not result.ok or not target.exists():
                raise ToolError(
                    "Screen capture failed. Screen Recording permission may be disabled.",
                    detail=result.output,
                )
            return target
        for cmd in (["grim", str(target)], ["scrot", str(target)],
                    ["import", "-window", "root", str(target)],
                    ["gnome-screenshot", "-f", str(target)]):
            if shutil.which(cmd[0]):
                result = await self.run(cmd, timeout=20.0)
                if result.ok and target.exists():
                    return target
        raise ToolError(
            "Screen capture isn't available on this host.",
            detail="no screencapture/grim/scrot binary found",
        )

    async def frontmost_window_id(self) -> str:
        result = await self.osascript(
            'tell application "System Events" to tell (first application process whose frontmost '
            "is true) to return value of attribute \"AXWindowNumber\" of front window"
        )
        return result.stdout.strip() if result.ok else "0"

    async def displays(self) -> list[dict]:
        if not self.is_macos:
            return []
        result = await self.run(
            ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"], timeout=25.0
        )
        if not result.ok:
            return []
        import json

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        displays: list[dict] = []
        for gpu in data.get("SPDisplaysDataType", []):
            for screen in gpu.get("spdisplays_ndrvs", []):
                displays.append(
                    {
                        "name": screen.get("_name", "Display"),
                        "resolution": screen.get("_spdisplays_resolution")
                        or screen.get("spdisplays_resolution", ""),
                        "main": screen.get("spdisplays_main") == "spdisplays_yes",
                    }
                )
        return displays

    # ------------------------------------------------------------------
    # notifications, sound
    # ------------------------------------------------------------------
    async def notify(self, title: str, message: str, subtitle: str = "") -> bool:
        if not self.is_macos:
            if shutil.which("notify-send"):
                return (await self.run(["notify-send", title, message], timeout=6.0)).ok
            return False
        script = (
            f'display notification "{_esc(message)}" with title "{_esc(title)}"'
            + (f' subtitle "{_esc(subtitle)}"' if subtitle else "")
        )
        return (await self.osascript(script, timeout=8.0)).ok

    async def get_volume(self) -> int | None:
        if not self.is_macos:
            return None
        result = await self.osascript("output volume of (get volume settings)")
        try:
            return int(result.stdout.strip())
        except (TypeError, ValueError):
            return None

    async def set_volume(self, level: int) -> bool:
        level = max(0, min(100, int(level)))
        if not self.is_macos:
            if shutil.which("pactl"):
                return (await self.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"], timeout=6.0
                )).ok
            return False
        return (await self.osascript(f"set volume output volume {level}")).ok

    async def set_muted(self, muted: bool) -> bool:
        if not self.is_macos:
            return False
        return (await self.osascript(
            f"set volume output muted {'true' if muted else 'false'}"
        )).ok

    # ------------------------------------------------------------------
    # permissions
    # ------------------------------------------------------------------
    async def check_permission(self, kind: str) -> tuple[bool, str]:
        """Probe a macOS privacy permission without triggering a scary prompt
        where avoidable. Returns ``(granted, explanation)``."""
        if not self.is_macos:
            return False, "not macOS"
        if kind == "accessibility":
            result = await self.osascript(
                'tell application "System Events" to return name of first application process '
                "whose frontmost is true"
            )
            return result.ok, result.stderr.strip()[:200] or "ok"
        if kind == "screen_recording":
            probe = Path(tempfile.gettempdir()) / "jarvis-perm-probe.png"
            result = await self.run(["/usr/sbin/screencapture", "-x", str(probe)], timeout=15.0)
            granted = result.ok and probe.exists() and probe.stat().st_size > 0
            probe.unlink(missing_ok=True)
            return granted, "ok" if granted else "Screen Recording permission is required"
        if kind in {"mail", "calendar", "automation"}:
            app = {"mail": "Mail", "calendar": "Calendar", "automation": "Finder"}[kind]
            result = await self.osascript(f'tell application "{app}" to return name', timeout=12.0)
            return result.ok, result.stderr.strip()[:200] or "ok"
        if kind == "microphone":
            return True, "verified at capture time"
        return False, f"unknown permission: {kind}"

    def open_privacy_settings(self, pane: str = "Privacy_Microphone") -> None:
        """Open the relevant System Settings pane (best effort, fire and forget)."""
        if not self.is_macos:
            return
        import subprocess

        with contextlib.suppress(OSError):
            subprocess.Popen(
                ["/usr/bin/open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _esc(text: str) -> str:
    """Escape a string for embedding in an AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_FALLBACK_CLIPBOARD = Path(tempfile.gettempdir()) / "jarvis-clipboard.txt"


def _fallback_clipboard_read() -> str:
    try:
        return _FALLBACK_CLIPBOARD.read_text(encoding="utf-8")
    except OSError:
        return ""


def _fallback_clipboard_write(text: str) -> None:
    with contextlib.suppress(OSError):
        _FALLBACK_CLIPBOARD.write_text(text, encoding="utf-8")
