"""Workspace file tools.

Risk is decided *per path*, not per tool: writing inside ``~/JARVIS`` is
ordinary work, writing into ``~/Documents`` asks first, and deleting always
asks. Deletes inside the workspace move the file to ``~/JARVIS/.trash`` so a
mistaken instruction is recoverable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ...core.errors import SandboxViolation
from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from ..system.info import format_bytes


class _SandboxTool(Tool):
    def __init__(self, deps):
        self._deps = deps

    @property
    def sandbox(self):
        return self._deps.sandbox

    async def _authorise(self, ctx: ToolContext, path: Path, *, write: bool = False,
                         delete: bool = False) -> Path:
        verdict = self.sandbox.classify(path, write=write, delete=delete)
        if verdict.risk != RiskLevel.LOW:
            verb = "Delete" if delete else ("Write to" if write else "Read")
            await ctx.permissions.require(
                action=f"file:{'delete' if delete else 'write' if write else 'read'}",
                risk=verdict.risk,
                summary=f"{verb} {verdict.path} ({verdict.reason})",
                details={"path": str(verdict.path), "reason": verdict.reason},
                # A file write inside an approved automation task (e.g. a
                # download) is routine, not consequential on its own — the
                # task grant covers it the same way it covers a click.
                # Delete never reaches here (DeleteFileTool doesn't call
                # _authorise; see its own always-consequential gating).
                task_id=ctx.task_id,
            )
        return verdict.path


class ListFilesTool(_SandboxTool):
    spec = ToolSpec(
        name="list_files",
        description="List files in the JARVIS workspace or a permitted folder",
        parameters={"type": "object", "properties": {"path": {"type": "string", "default": ""}}},
        risk=RiskLevel.LOW,
        category="files",
        expected_ms=60,
        examples=["What's in my workspace?", "List my notes"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = await self._authorise(ctx, self.sandbox.resolve(args.get("path", "")))
        entries = self.sandbox.listing(path)
        rows = [[e["name"], e["type"], format_bytes(e["size"]) if e["type"] == "file" else "—"]
                for e in entries]
        return ToolResult(
            data={"path": str(path), "entries": entries},
            summary=f"{len(entries)} item{'s' if len(entries) != 1 else ''} in {path.name or path}.",
            display={"kind": "table", "title": str(path), "columns": ["Name", "Type", "Size"],
                     "rows": rows},
        )


class ReadFileTool(_SandboxTool):
    spec = ToolSpec(
        name="read_file",
        description="Read a text file from the workspace or a permitted folder",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "max_chars": {"type": "integer", "default": 20000}},
            "required": ["path"],
        },
        risk=RiskLevel.LOW,
        category="files",
        expected_ms=60,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = await self._authorise(ctx, self.sandbox.resolve(args["path"]))
        text = self.sandbox.read_text(path, int(args.get("max_chars", 20000)))
        return ToolResult(
            data={"path": str(path), "text": text, "chars": len(text)},
            summary=f"Read {path.name} — {len(text)} characters.",
            display={"kind": "code", "title": path.name, "text": text[:6000]},
        )


class WriteFileTool(_SandboxTool):
    spec = ToolSpec(
        name="write_file",
        description="Create or overwrite a file (inside the workspace by default)",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        },
        risk=RiskLevel.LOW,  # escalated per-path inside run()
        category="files",
        expected_ms=80,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self.sandbox.resolve(args["path"], for_write=True)
        path = await self._authorise(ctx, target, write=True)
        existed = path.exists()
        written = self.sandbox.write_text(path, args["content"], append=bool(args.get("append")))
        verb = "Appended to" if args.get("append") else ("Updated" if existed else "Created")
        return ToolResult(
            data={"path": str(path), "bytes": written},
            summary=f"{verb} {path.name}.",
            display={"kind": "file", "title": path.name, "path": str(path),
                     "preview": args["content"][:1200]},
        )


class CreateNoteTool(_SandboxTool):
    spec = ToolSpec(
        name="create_note",
        description="Write a dated note into the workspace notes folder",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string", "default": ""},
                           "content": {"type": "string"}},
            "required": ["content"],
        },
        risk=RiskLevel.LOW,
        category="files",
        expected_ms=80,
        examples=["Make a note that the deploy is on Friday"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        now = dt.datetime.now()
        title = (args.get("title") or args["content"][:40]).strip()
        slug = "".join(c if c.isalnum() or c in "- " else "" for c in title).strip().replace(" ", "-")
        notes_dir = self._deps.config.notes_dir
        notes_dir.mkdir(parents=True, exist_ok=True)
        path = notes_dir / f"{now:%Y-%m-%d}-{slug.lower() or 'note'}.md"
        body = f"# {title}\n\n_{now:%A %d %B %Y, %H:%M}_\n\n{args['content']}\n"
        self.sandbox.write_text(path, body, append=path.exists())
        return ToolResult(
            data={"path": str(path)},
            summary=f"Noted — saved as {path.name}.",
            display={"kind": "file", "title": title, "path": str(path), "preview": body},
        )


class SearchFilesTool(_SandboxTool):
    spec = ToolSpec(
        name="search_files",
        description="Search the workspace by filename or file contents",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["name", "content", "both"], "default": "both"},
                "path": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="files",
        expected_ms=400,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = await self._authorise(ctx, self.sandbox.resolve(args.get("path", "")))
        mode = args.get("mode", "both")
        hits: list[dict] = []
        if mode in {"name", "both"}:
            hits += self.sandbox.search(args["query"], root)
        if mode in {"content", "both"}:
            hits += self.sandbox.grep(args["query"], root)
        seen: set[str] = set()
        unique = [h for h in hits if not (h["path"] in seen or seen.add(h["path"]))]
        return ToolResult(
            data={"hits": unique},
            summary=f"{len(unique)} match{'es' if len(unique) != 1 else ''} for “{args['query']}”.",
            display={"kind": "table", "title": f"Search: {args['query']}",
                     "columns": ["File", "Where", "Excerpt"],
                     "rows": [[h["name"], h["match"], h.get("excerpt", "")[:120]] for h in unique[:25]]},
        )


class MoveFileTool(_SandboxTool):
    spec = ToolSpec(
        name="move_file",
        description="Move or rename a file inside the permitted areas",
        parameters={
            "type": "object",
            "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
            "required": ["source", "destination"],
        },
        risk=RiskLevel.MEDIUM,
        category="files",
        expected_ms=100,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        source = await self._authorise(ctx, self.sandbox.resolve(args["source"]), write=True)
        destination = await self._authorise(
            ctx, self.sandbox.resolve(args["destination"], for_write=True), write=True
        )
        if not source.exists():
            raise SandboxViolation("I couldn't find that file.", detail=str(source))
        final = self.sandbox.move(source, destination)
        return ToolResult(data={"path": str(final)}, summary=f"Moved to {final.name}.")


class DeleteFileTool(_SandboxTool):
    spec = ToolSpec(
        name="delete_file",
        description="Delete a file from the workspace (moved to the workspace trash)",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        risk=RiskLevel.HIGH,
        category="files",
        expected_ms=80,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = self.sandbox.resolve(args["path"])
        verdict = self.sandbox.classify(path, delete=True)
        if not path.exists():
            return ToolResult.failure("There's no such file.", detail=str(path))
        self.sandbox.delete(verdict.path)
        return ToolResult(
            data={"path": str(path)},
            summary=f"Deleted {path.name} — it's in the workspace trash if you need it back.",
        )


class WorkspaceInfoTool(_SandboxTool):
    spec = ToolSpec(
        name="workspace_info",
        description="Describe the JARVIS workspace: location, size and contents",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="files",
        expected_ms=80,
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = self.sandbox.root
        entries = self.sandbox.listing(root)
        total = 0
        count = 0
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                    count += 1
                except OSError:
                    continue
        return ToolResult(
            data={"root": str(root), "files": count, "bytes": total, "entries": entries},
            summary=f"Your workspace is at {root}, holding {count} files, {format_bytes(total)}.",
            display={"kind": "facts", "title": "Workspace",
                     "facts": [("Location", str(root)), ("Files", str(count)),
                               ("Size", format_bytes(total))]},
        )


def file_tools(deps) -> list[Tool]:
    return [
        ListFilesTool(deps),
        ReadFileTool(deps),
        WriteFileTool(deps),
        CreateNoteTool(deps),
        SearchFilesTool(deps),
        MoveFileTool(deps),
        DeleteFileTool(deps),
        WorkspaceInfoTool(deps),
    ]
