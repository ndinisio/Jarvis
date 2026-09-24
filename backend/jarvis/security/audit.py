"""What JARVIS did, action by action — kept, so it can be checked afterwards.

Every tool call goes through the registry, and the registry writes one line
per call here: when, which tool, the arguments (secrets redacted), what the
real target was, whether it was consequential, how it was allowed (a setting,
the autonomy level, a grant, or the user saying yes) or that it was declined
or refused, and what came of it. One file per task — or per spoken request
outside a task — under ``~/JARVIS/audit/<date>/``, as JSON lines anyone can
read. With ``security.audit_screenshots`` on, an action on a page in JARVIS
Chrome also keeps a small picture of the page afterwards.

Records older than ``security.audit_days`` are removed at start-up.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ..core.logging import get_logger

log = get_logger("jarvis.audit")

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


class AuditLog:
    def __init__(self, root: Path, config_store):
        self.root = Path(root)
        self._config_store = config_store

    @property
    def enabled(self) -> bool:
        return bool(self._config_store.current.security.audit)

    @property
    def screenshots(self) -> bool:
        return self.enabled and bool(self._config_store.current.security.audit_screenshots)

    # -- writing ------------------------------------------------------------------
    def record(self, key: str, entry: dict[str, Any]) -> None:
        """Add *entry* to the record for *key* (a task id or a turn id)."""
        if not self.enabled or not key:
            return
        line = json.dumps({"ts": round(time.time(), 3), **entry}, ensure_ascii=False, default=str)
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:  # pragma: no cover - disk trouble shouldn't stop the action
            log.warning("audit record for %s not written: %s", key, exc)

    def save_picture(self, key: str, data: bytes, suffix: str = "jpg") -> str:
        """Keep a picture for *key*; returns its file name (for the record)."""
        folder = self._path(key).with_suffix("")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}.{suffix}"
            (folder / name).write_bytes(data)
            return f"{folder.name}/{name}"
        except OSError as exc:  # pragma: no cover
            log.warning("audit picture for %s not saved: %s", key, exc)
            return ""

    # -- reading ------------------------------------------------------------------
    def entries(self, key: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        name = f"{_SAFE.sub('_', key)}.jsonl"
        if not self.root.exists():
            return out
        for day in sorted(self.root.iterdir()):
            path = day / name
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        return out

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The latest records: key, day, number of actions, last action."""
        if not self.root.exists():
            return []
        files = sorted(self.root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        summary = []
        for path in files[:limit]:
            lines = path.read_text(encoding="utf-8").splitlines()
            last = json.loads(lines[-1]) if lines else {}
            summary.append({"key": path.stem, "day": path.parent.name, "actions": len(lines),
                            "last": last.get("tool", ""), "ts": last.get("ts")})
        return summary

    # -- housekeeping -------------------------------------------------------------
    def prune(self) -> int:
        """Remove days older than ``security.audit_days``."""
        days = int(self._config_store.current.security.audit_days or 0)
        if days <= 0 or not self.root.exists():
            return 0
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        removed = 0
        for day in self.root.iterdir():
            if day.is_dir() and day.name < cutoff:
                shutil.rmtree(day, ignore_errors=True)
                removed += 1
        return removed

    def _path(self, key: str) -> Path:
        return self.root / time.strftime("%Y-%m-%d") / f"{_SAFE.sub('_', key)}.jsonl"
