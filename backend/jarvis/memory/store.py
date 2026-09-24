"""Local persistent memory.

Three separate things, deliberately not mixed:

``preferences``   small durable settings the user states ("call me sir", voice)
``facts``         long-term knowledge worth keeping ("my Mac is an M3 Pro")
``messages``      conversation history, used for *recent* context only

Everything lives in one SQLite file inside the workspace, so it is local,
inspectable (``sqlite3 ~/JARVIS/memory/jarvis.db``), editable and deletable.
Nothing here is uploaded anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.logging import get_logger

log = get_logger("jarvis.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    tags TEXT DEFAULT '',
    source TEXT DEFAULT 'conversation',
    importance REAL DEFAULT 0.5,
    created REAL NOT NULL,
    last_used REAL DEFAULT 0,
    uses INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY,
    kind TEXT,
    title TEXT,
    status TEXT,
    started REAL,
    finished REAL,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_facts_created ON facts(created);
"""

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "my", "your", "i", "you", "me", "to", "of",
    "and", "or", "in", "on", "for", "that", "this", "it", "do", "does", "did", "what", "how",
    "please", "jarvis", "can", "could", "would", "about", "with", "have", "has",
}


@dataclass(slots=True)
class Fact:
    id: int
    text: str
    tags: str = ""
    importance: float = 0.5
    created: float = 0.0
    source: str = "conversation"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "tags": self.tags,
                "importance": self.importance, "created": self.created, "source": self.source}


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Write-ahead logging: readers never wait on a writer, and a one-shot
        # `jarvis ask` can use the database while the assistant is running.
        # NORMAL sync is the standard pairing — safe against a crash of
        # JARVIS itself; only a power cut can lose the last few writes.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- plumbing ----------------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # -- preferences -------------------------------------------------------
    def set_preference_sync(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO preferences(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value), time.time()),
        )

    async def set_preference(self, key: str, value: Any) -> None:
        await self._run(self.set_preference_sync, key, value)

    def get_preference(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def preferences(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT key, value FROM preferences ORDER BY key").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                out[row["key"]] = row["value"]
        return out

    async def forget_preference(self, key: str) -> bool:
        cursor = await self._run(self._execute, "DELETE FROM preferences WHERE key=?", (key,))
        return cursor.rowcount > 0

    # -- facts -------------------------------------------------------------
    def remember_sync(self, text: str, tags: str = "", importance: float = 0.5,
                      source: str = "conversation") -> int:
        text = text.strip()
        if not text:
            return 0
        existing = self._conn.execute(
            "SELECT id FROM facts WHERE lower(text)=lower(?)", (text,)
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = self._execute(
            "INSERT INTO facts(text, tags, source, importance, created) VALUES(?,?,?,?,?)",
            (text, tags, source, importance, time.time()),
        )
        return int(cursor.lastrowid)

    async def remember(self, text: str, tags: str = "", importance: float = 0.5,
                       source: str = "conversation") -> int:
        return await self._run(self.remember_sync, text, tags, importance, source)

    def facts(self, limit: int = 100) -> list[Fact]:
        rows = self._conn.execute(
            "SELECT * FROM facts ORDER BY importance DESC, created DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_as_fact(r) for r in rows]

    def relevant_facts(self, query: str, limit: int = 6) -> list[Fact]:
        """Keyword-overlap retrieval — cheap, predictable and good enough that a
        local model never sees the whole memory store."""
        terms = _keywords(query)
        rows = self._conn.execute("SELECT * FROM facts").fetchall()
        if not rows:
            return []
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            haystack = f"{row['text']} {row['tags']}".lower()
            hits = sum(1 for term in terms if term in haystack)
            if not hits:
                continue
            recency = 1.0 / (1.0 + max(0.0, (time.time() - row["created"]) / 86400.0) / 30.0)
            scored.append((hits * 2 + row["importance"] + recency, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [_as_fact(row) for _, row in scored[:limit]]
        if chosen:
            ids = tuple(f.id for f in chosen)
            placeholders = ",".join("?" * len(ids))
            self._execute(
                f"UPDATE facts SET uses = uses + 1, last_used = ? WHERE id IN ({placeholders})",
                (time.time(), *ids),
            )
        return chosen

    async def forget(self, query: str) -> list[str]:
        """Delete facts matching *query*. Returns what was removed."""

        def _forget() -> list[str]:
            terms = _keywords(query)
            rows = self._conn.execute("SELECT * FROM facts").fetchall()
            removed: list[str] = []
            for row in rows:
                haystack = row["text"].lower()
                if query.strip().lower() in haystack or (
                    terms and all(term in haystack for term in terms)
                ):
                    self._execute("DELETE FROM facts WHERE id=?", (row["id"],))
                    removed.append(row["text"])
            return removed

        return await self._run(_forget)

    async def forget_fact(self, fact_id: int) -> bool:
        cursor = await self._run(self._execute, "DELETE FROM facts WHERE id=?", (fact_id,))
        return cursor.rowcount > 0

    async def clear_facts(self) -> int:
        cursor = await self._run(self._execute, "DELETE FROM facts", ())
        return cursor.rowcount

    # -- conversation ------------------------------------------------------
    def add_message_sync(self, role: str, text: str, meta: dict | None = None) -> None:
        self._execute(
            "INSERT INTO messages(role, text, ts, meta) VALUES(?,?,?,?)",
            (role, text, time.time(), json.dumps(meta or {})),
        )

    async def add_message(self, role: str, text: str, meta: dict | None = None) -> None:
        await self._run(self.add_message_sync, role, text, meta)

    def recent_messages(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT role, text, ts, meta FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in reversed(rows):
            out.append({"role": row["role"], "text": row["text"], "ts": row["ts"]})
        return out

    async def clear_conversation(self) -> int:
        cursor = await self._run(self._execute, "DELETE FROM messages", ())
        return cursor.rowcount

    # -- tasks -------------------------------------------------------------
    async def log_task(self, task_id: str, kind: str, title: str, status: str,
                       started: float, finished: float | None, result: str = "") -> None:
        await self._run(
            self._execute,
            "INSERT INTO task_log(id, kind, title, status, started, finished, result) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "finished=excluded.finished, result=excluded.result",
            (task_id, kind, title, status, started, finished, result[:4000]),
        )

    def task_history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_log ORDER BY started DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- introspection -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "preferences": self.preferences(),
            "facts": [f.as_dict() for f in self.facts()],
            "message_count": self._conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"],
            "path": str(self.path),
        }

    def describe(self) -> str:
        """Answer for "what do you remember about me?" — spoken, not a data dump."""
        preferences = self.preferences()
        facts = self.facts(limit=8)
        if not preferences and not facts:
            return "Nothing yet, sir. Tell me anything worth keeping and I'll hold on to it."
        parts: list[str] = []
        name = preferences.get("preferred_name")
        if name:
            parts.append(f"You prefer to be called {name}")
        for fact in facts[:5]:
            parts.append(fact.text.rstrip("."))
        extra = len(facts) - 5
        text = ". ".join(parts) + "."
        if extra > 0:
            text += f" There are {extra} further notes on file."
        return text

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover
            self._conn.close()


def _as_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=int(row["id"]), text=row["text"], tags=row["tags"] or "",
        importance=float(row["importance"] or 0.5), created=float(row["created"] or 0),
        source=row["source"] or "conversation",
    )


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS][:12]
