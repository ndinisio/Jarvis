"""The file sandbox: the workspace is free, everything else is graded."""

from __future__ import annotations

from pathlib import Path

import pytest
from jarvis.core.errors import SandboxViolation
from jarvis.security.permissions import RiskLevel


def test_workspace_paths_are_low_risk(app):
    sandbox = app.sandbox
    path = sandbox.resolve("notes/today.md")
    verdict = sandbox.classify(path, write=True)
    assert verdict.inside_workspace is True
    assert verdict.risk == RiskLevel.LOW


def test_relative_paths_resolve_inside_the_workspace(app):
    path = app.sandbox.resolve("report.md")
    assert str(path).startswith(str(app.sandbox.root))


def test_escaping_the_workspace_is_graded_not_silent(app):
    sandbox = app.sandbox
    home_file = Path.home() / "some-private-file.txt"
    verdict = sandbox.classify(home_file, write=False)
    assert verdict.inside_workspace is False
    assert verdict.risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}


def test_system_paths_are_refused_outright(app):
    for path in ["/System/Library/x", "/usr/bin/python3", "/etc/hosts"]:
        with pytest.raises(SandboxViolation):
            app.sandbox.classify(Path(path), write=True)


def test_traversal_cannot_escape(app):
    sandbox = app.sandbox
    resolved = sandbox.resolve("../../../../etc/passwd")
    assert sandbox.is_inside_workspace(resolved) is False
    with pytest.raises(SandboxViolation):
        sandbox.classify(resolved, write=True)


def test_delete_outside_the_workspace_is_refused(app, tmp_path):
    with pytest.raises(SandboxViolation):
        app.sandbox.classify(tmp_path / "elsewhere.txt", delete=True)


async def test_write_read_and_search_round_trip(app, ctx):
    registry = app.deps.registry
    write = await registry.call(
        "write_file", {"path": "notes/plan.md", "content": "# Plan\nShip V1 on Friday."}, ctx
    )
    assert write.ok

    listing = await registry.call("list_files", {"path": "notes"}, ctx)
    assert listing.ok and any(e["name"] == "plan.md" for e in listing.data["entries"])

    read = await registry.call("read_file", {"path": "notes/plan.md"}, ctx)
    assert "Ship V1" in read.data["text"]

    found = await registry.call("search_files", {"query": "Friday", "mode": "content"}, ctx)
    assert found.ok and found.data["hits"]


async def test_delete_moves_to_workspace_trash(app, ctx):
    app.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                          "always_confirm": []}})
    await app.deps.registry.call("write_file", {"path": "scratch.txt", "content": "x"}, ctx)
    result = await app.deps.registry.call("delete_file", {"path": "scratch.txt"}, ctx)
    assert result.ok
    assert not (app.sandbox.root / "scratch.txt").exists()
    assert (app.sandbox.root / ".trash" / "scratch.txt").exists()


async def test_create_note_is_dated(app, ctx):
    result = await app.deps.registry.call(
        "create_note", {"content": "The deploy is on Friday."}, ctx
    )
    assert result.ok
    path = Path(result.data["path"])
    assert path.exists() and path.suffix == ".md"
    assert "The deploy is on Friday." in path.read_text()
