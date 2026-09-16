"""The filesystem permission boundary.

``~/JARVIS`` is JARVIS' own workspace: full read/write, no questions asked.
Everything else is graded:

* configured *readable roots* (``~/Documents`` and friends) — readable, writes
  require confirmation
* anywhere else in the home directory — read requires confirmation, write is
  HIGH risk
* system paths — refused outright

Deletion is always HIGH risk, and symlinks are resolved before the check so a
link can't be used to step outside the boundary.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ...core.config import Config
from ...core.errors import SandboxViolation
from ...security.permissions import RiskLevel

#: Never touched, whatever the user asks.
FORBIDDEN_PREFIXES = (
    "/System", "/usr", "/bin", "/sbin", "/private/var/db", "/Library/LaunchDaemons",
    "/Library/LaunchAgents", "/etc", "/var/root", "/Applications",
)

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv",
    ".tsv", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".zsh", ".rb", ".go", ".rs",
    ".c", ".h", ".cpp", ".swift", ".java", ".html", ".css", ".xml", ".sql", ".env", ".conf",
}


@dataclass(slots=True)
class PathVerdict:
    path: Path
    inside_workspace: bool
    risk: str
    reason: str


class FileSandbox:
    def __init__(self, config: Config):
        self._config = config
        self.root = config.workspace_path

    def reconfigure(self, config: Config) -> None:
        self._config = config
        self.root = config.workspace_path

    # -- resolution --------------------------------------------------------
    def resolve(self, raw: str, *, for_write: bool = False) -> Path:
        """Turn user input into an absolute path, defaulting to the workspace."""
        raw = (raw or "").strip()
        if not raw:
            return self.root
        expanded = Path(os.path.expanduser(os.path.expandvars(raw)))
        if not expanded.is_absolute():
            expanded = self.root / expanded
        try:
            resolved = expanded.resolve()
        except (OSError, RuntimeError) as exc:
            raise SandboxViolation("That path can't be resolved.", detail=str(exc)) from exc
        if for_write and self.is_inside_workspace(resolved):
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def is_inside_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except (ValueError, OSError):
            return False

    def _readable_roots(self) -> list[Path]:
        roots = [self.root]
        for raw in self._config.security.readable_roots:
            try:
                roots.append(Path(os.path.expanduser(raw)).resolve())
            except OSError:
                continue
        return roots

    def classify(self, path: Path, *, write: bool = False, delete: bool = False) -> PathVerdict:
        """Grade an operation on *path*; raises for outright-forbidden paths."""
        resolved = path.resolve() if path.exists() else path
        text = str(resolved)
        for prefix in FORBIDDEN_PREFIXES:
            if text == prefix or text.startswith(prefix + "/"):
                raise SandboxViolation(
                    "That's a protected system location — I won't touch it.", detail=text
                )
        if self.is_inside_workspace(resolved):
            risk = RiskLevel.HIGH if delete else RiskLevel.LOW
            return PathVerdict(resolved, True, risk, "inside the JARVIS workspace")
        if delete:
            raise SandboxViolation(
                "I only delete files inside my own workspace.", detail=text
            )
        for root in self._readable_roots():
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return PathVerdict(
                resolved, False, RiskLevel.HIGH if write else RiskLevel.LOW,
                f"inside the permitted folder {root.name}",
            )
        home = Path.home().resolve()
        try:
            resolved.relative_to(home)
        except ValueError:
            raise SandboxViolation(
                "That's outside the areas I'm allowed to work in.", detail=text
            ) from None
        return PathVerdict(
            resolved, False, RiskLevel.HIGH if write else RiskLevel.MEDIUM,
            "inside your home folder but outside my workspace",
        )

    # -- operations --------------------------------------------------------
    def read_text(self, path: Path, max_chars: int = 40_000) -> str:
        if path.is_dir():
            raise SandboxViolation("That's a folder, not a file.", detail=str(path))
        if not path.exists():
            raise SandboxViolation("I couldn't find that file.", detail=str(path))
        if path.stat().st_size > 8 * 1024 * 1024:
            raise SandboxViolation("That file is too large to read in one go.", detail=str(path))
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[:max_chars]

    def write_text(self, path: Path, content: str, append: bool = False) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(content)
        return len(content)

    def listing(self, path: Path, limit: int = 200) -> list[dict]:
        if not path.exists():
            raise SandboxViolation("That folder doesn't exist.", detail=str(path))
        if path.is_file():
            stat = path.stat()
            return [{"name": path.name, "type": "file", "size": stat.st_size,
                     "modified": stat.st_mtime, "path": str(path)}]
        entries: list[dict] = []
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": entry.name,
                    "type": "folder" if entry.is_dir() else "file",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "path": str(entry),
                }
            )
            if len(entries) >= limit:
                break
        return entries

    def search(self, query: str, root: Path | None = None, limit: int = 40) -> list[dict]:
        root = root or self.root
        query_lower = query.lower()
        hits: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")][:50]
            for name in filenames:
                if name.startswith("."):
                    continue
                if query_lower in name.lower():
                    full = Path(dirpath) / name
                    try:
                        stat = full.stat()
                    except OSError:
                        continue
                    hits.append({"name": name, "path": str(full), "size": stat.st_size,
                                 "modified": stat.st_mtime, "match": "name"})
                    if len(hits) >= limit:
                        return hits
        return hits

    def grep(self, query: str, root: Path | None = None, limit: int = 20) -> list[dict]:
        """Content search across text files in the workspace."""
        root = root or self.root
        query_lower = query.lower()
        hits: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                try:
                    if path.stat().st_size > 2 * 1024 * 1024:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                index = text.lower().find(query_lower)
                if index >= 0:
                    start = max(0, index - 80)
                    hits.append(
                        {
                            "path": str(path),
                            "name": name,
                            "excerpt": text[start : index + 160].replace("\n", " ").strip(),
                            "match": "content",
                        }
                    )
                    if len(hits) >= limit:
                        return hits
        return hits

    def move(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        return Path(shutil.move(str(source), str(destination)))

    def delete(self, path: Path) -> bool:
        """Delete inside the workspace by moving to a trash folder — recoverable."""
        trash = self.root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / path.name
        counter = 1
        while target.exists():
            target = trash / f"{path.stem}-{counter}{path.suffix}"
            counter += 1
        shutil.move(str(path), str(target))
        return True
