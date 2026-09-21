"""Downloading a file to disk.

The automation examples this feature was built for both end with a file on
the user's Mac — "download the Python installer", "download this driver" —
and until now there was no such primitive anywhere in the codebase: the web
tools only ever read a page's text, never bytes to disk.

Reuses the same permission boundary every other file operation goes
through — :class:`~jarvis.tools.files.tools._SandboxTool` — so a download
into ``~/Downloads`` is graded exactly the way writing there with
``write_file`` already is, and ``run_installer`` (``installer.py``, right
next to this file) is what actually *runs* a downloaded file, kept
deliberately separate so downloading is never itself the consequential
step.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from ...security.permissions import RiskLevel
from ..base import ToolContext, ToolResult, ToolSpec
from ..files.tools import _SandboxTool
from ..system.info import format_bytes

_ALLOWED_SCHEMES = {"http", "https"}


class DownloadFileTool(_SandboxTool):
    spec = ToolSpec(
        name="download_file",
        description="Download a file from a URL to disk (the Downloads folder by default)",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "filename": {"type": "string", "default": "",
                            "description": "overrides the name the server or URL suggest"},
                "destination": {"type": "string", "default": "",
                                "description": "a folder; defaults to ~/Downloads"},
            },
            "required": ["url"],
        },
        risk=RiskLevel.LOW,  # escalated per-path inside run(), same convention as write_file
        category="files",
        requires_network=True,
        mutates=True,
        retryable=False,
        expected_ms=4000,
        examples=["download the latest python installer", "download this file",
                 "save that driver to my downloads folder"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        scheme = urlsplit(url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return ToolResult.failure("I need a proper http or https URL to download.")

        destination_dir = self.sandbox.resolve(args.get("destination") or "~/Downloads",
                                                for_write=True)
        requested_name = _safe_filename(args.get("filename") or "")
        max_bytes = self._deps.config.automation.max_download_mb * 1024 * 1024

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client, \
                       client.stream("GET", url) as response:
                response.raise_for_status()
                filename = requested_name or _filename_from(url, response)
                target = await self._authorise(ctx, destination_dir / filename, write=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                target = _avoid_collision(target)

                written = 0
                partial = target.with_name(target.name + ".part")
                try:
                    with partial.open("wb") as fh:
                        async for chunk in response.aiter_bytes(65536):
                            written += len(chunk)
                            if written > max_bytes:
                                raise _TooLarge()
                            fh.write(chunk)
                except _TooLarge:
                    partial.unlink(missing_ok=True)
                    limit = self._deps.config.automation.max_download_mb
                    return ToolResult.failure(
                        f"That file is larger than the {limit} MB limit — I stopped partway "
                        "through rather than fill your disk."
                    )
                partial.replace(target)
        except httpx.HTTPStatusError as exc:
            return ToolResult.failure(
                f"The download failed — the server said {exc.response.status_code}.",
                detail=str(exc),
            )
        except httpx.HTTPError as exc:
            return ToolResult.failure("The download didn't complete.", detail=str(exc))

        return ToolResult(
            data={"path": str(target), "bytes": written, "url": url},
            summary=f"Downloaded {target.name} ({format_bytes(written)}).",
            display={"kind": "file", "title": target.name, "path": str(target)},
        )


class _TooLarge(Exception):
    pass


def _safe_filename(name: str) -> str:
    """Strip any directory component and reject path traversal — a
    filename (whether given by the caller or read from a response header)
    must never be able to redirect the write outside the destination
    directory that was just authorised."""
    name = (name or "").strip()
    if not name:
        return ""
    name = Path(name).name  # drops any leading "../" or "/" component
    if not name or name in {".", ".."}:
        return ""
    return name


def _filename_from(url: str, response: httpx.Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    if match:
        candidate = _safe_filename(unquote(match.group(1)))
        if candidate:
            return candidate
    from_url = _safe_filename(Path(urlsplit(url).path).name)
    return from_url or "download"


def _avoid_collision(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 1000):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    return path  # pragma: no cover - exhausted 999 collisions


def download_tools(deps) -> list:
    return [DownloadFileTool(deps)]
