"""Loading the evaluation suites (YAML) into typed records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent


@dataclass
class WebTask:
    id: str
    category: str
    phrasings: list[str]
    checks: list[dict[str, Any]]
    setup: dict[str, Any] = field(default_factory=dict)
    start_url: str = ""
    #: Substrings of a confirmation prompt the simulated user says yes to.
    #: Anything consequential not listed here is declined — which is what a
    #: safety task relies on.
    approve: list[str] = field(default_factory=list)
    oracle: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: Which phase of v3.0 is expected to make this pass (informational).
    target_phase: int = 1


@dataclass
class Utterance:
    text: str
    mode: str                      # "act" | "chat"
    quick: str | None = None       # tool/capability name, "none", or None (no expectation)
    norm: str | None = None        # expected deterministic tool after normalisation (Phase 3)
    tags: list[str] = field(default_factory=list)


@dataclass
class MacTask:
    id: str
    utterance: str
    check: str
    setup: str = ""
    cleanup: str = ""
    approve: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def load_web_tasks(path: Path | None = None) -> list[WebTask]:
    data = yaml.safe_load((path or ROOT / "web_tasks.yaml").read_text(encoding="utf-8"))
    tasks = []
    for raw in data["tasks"]:
        tasks.append(WebTask(
            id=raw["id"], category=raw.get("category", "general"),
            phrasings=list(raw["phrasings"]), checks=list(raw.get("checks", [])),
            setup=raw.get("setup") or {}, start_url=raw.get("start_url", ""),
            approve=list(raw.get("approve", [])), oracle=list(raw.get("oracle", [])),
            tags=list(raw.get("tags", [])), target_phase=int(raw.get("target_phase", 1)),
        ))
    ids = [t.id for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return tasks


def load_utterances(path: Path | None = None) -> list[Utterance]:
    data = yaml.safe_load((path or ROOT / "utterances.yaml").read_text(encoding="utf-8"))
    out = []
    for group in data["groups"]:
        tags = list(group.get("tags", []))
        for raw in group["utterances"]:
            if isinstance(raw, str):
                raw = {"t": raw}
            out.append(Utterance(
                text=raw["t"], mode=raw.get("mode", group.get("mode", "act")),
                quick=_quick(raw.get("quick", group.get("quick"))),
                norm=raw.get("norm", group.get("norm")),
                tags=tags + list(raw.get("tags", [])),
            ))
    return out


def load_mac_tasks(path: Path | None = None) -> list[MacTask]:
    data = yaml.safe_load((path or ROOT / "mac_tasks.yaml").read_text(encoding="utf-8"))
    return [MacTask(id=raw["id"], utterance=raw["utterance"], check=raw["check"],
                    setup=raw.get("setup", ""), cleanup=raw.get("cleanup", ""),
                    approve=list(raw.get("approve", [])), tags=list(raw.get("tags", [])))
            for raw in data["tasks"]]


def _quick(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
