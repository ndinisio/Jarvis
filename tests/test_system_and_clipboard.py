"""Deterministic answers: system facts and the clipboard must not need a model."""

from __future__ import annotations

from jarvis.tools.clipboard.tools import looks_sensitive
from jarvis.tools.system.info import format_bytes


def test_format_bytes_reads_like_speech():
    assert format_bytes(412 * 1000**3) == "412.0 GB".replace(".0 ", " ")
    assert format_bytes(0) == "0 bytes"
    assert format_bytes(16 * 1000**3).endswith("GB")


async def test_system_info_is_structured(app, ctx):
    result = await app.deps.registry.call("get_system_info", {}, ctx)
    assert result.ok
    assert result.data["os_version"]
    assert result.summary


async def test_storage_answers_without_a_model(app, ctx, fake_provider):
    result = await app.deps.registry.call("get_storage", {}, ctx)
    assert result.ok
    assert "available" in result.summary
    assert result.data["total_bytes"] > 0
    assert fake_provider.calls == []  # no model was consulted


async def test_system_queries_are_quick(app, ctx):
    for tool in ("get_time", "get_storage", "get_system_info", "get_memory"):
        result = await app.deps.registry.call(tool, {}, ctx)
        assert result.ok
        assert result.duration_ms < 2500, f"{tool} took {result.duration_ms:.0f} ms"


async def test_time_tool_fields(app, ctx):
    both = await app.deps.registry.call("get_time", {}, ctx)
    assert " on " in both.summary
    only_time = await app.deps.registry.call("get_time", {"field": "time"}, ctx)
    assert only_time.summary.startswith("It's")


async def test_clipboard_round_trip(app, ctx):
    write = await app.deps.registry.call("write_clipboard", {"text": "the quick brown fox"}, ctx)
    assert write.ok
    read = await app.deps.registry.call("read_clipboard", {}, ctx)
    assert read.data["text"] == "the quick brown fox"
    assert "quick brown fox" in read.summary


async def test_clipboard_append(app, ctx):
    await app.deps.registry.call("write_clipboard", {"text": "one"}, ctx)
    await app.deps.registry.call("append_clipboard", {"text": "two"}, ctx)
    read = await app.deps.registry.call("read_clipboard", {}, ctx)
    assert read.data["text"].splitlines() == ["one", "two"]


async def test_sensitive_clipboard_is_not_read_aloud(app, ctx):
    secret = "api_key=sk-9f8a7b6c5d4e3f2a1b0c9d8e"
    await app.deps.registry.call("write_clipboard", {"text": secret}, ctx)
    read = await app.deps.registry.call("read_clipboard", {}, ctx)
    assert read.data["sensitive"] is True
    assert secret not in read.summary
    assert read.display["sensitive"] is True


def test_secret_detection():
    assert looks_sensitive("password: hunter2")
    assert looks_sensitive("-----BEGIN RSA PRIVATE KEY-----")
    assert looks_sensitive("token: ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert looks_sensitive("my api key is sk-proj-abcdefghijklmnop123456")
    assert looks_sensitive("4111 1111 1111 1111")
    assert not looks_sensitive("Remember to buy milk")
    assert not looks_sensitive("the token was rotated this morning")


async def test_diagnostics_produces_structured_findings(app, ctx):
    result = await app.deps.registry.call("run_diagnostics", {"areas": ["storage", "memory"]}, ctx)
    assert result.ok
    findings = result.data["findings"]
    assert findings and all({"severity", "area", "observation"} <= set(f) for f in findings)
    assert result.data["severity"] in {"ok", "info", "warning", "critical"}


async def test_processes_tool(app, ctx):
    result = await app.deps.registry.call("get_processes", {"limit": 3}, ctx)
    assert result.ok
    assert len(result.data["processes"]) <= 3
