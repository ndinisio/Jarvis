"""System information and diagnostics tools.

These answer deterministically. No model is consulted to find out how much
storage is free — macOS already knows, and it answers in milliseconds.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .info import format_bytes


class SystemInfoTool(Tool):
    spec = ToolSpec(
        name="get_system_info",
        description="Machine model, chip, cores, memory size, macOS version and uptime",
        parameters={
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["all", "model", "chip", "memory", "os", "uptime", "cores"],
                    "default": "all",
                }
            },
        },
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=60,
        examples=["What chip does this Mac have?", "What macOS version am I running?"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = await self._deps.sysinfo.overview()
        field = args.get("field", "all")
        os_label = f"{data.get('platform', 'macOS')} {data.get('os_version', '')}".strip()
        if data.get("os_name"):
            os_label += f" ({data['os_name']})"
        phrases = {
            "model": f"This is a {data.get('model') or data.get('model_identifier')}.",
            "chip": f"It's running a {data.get('chip')}.",
            "memory": f"You have {format_bytes(data.get('memory_bytes', 0))} of memory.",
            "os": f"You're on {os_label}.",
            "uptime": f"It's been up for {data.get('uptime')}.",
            "cores": f"{data.get('cores')} CPU cores"
            + (
                f" — {data.get('performance_cores')} performance and "
                f"{data.get('efficiency_cores')} efficiency."
                if data.get("performance_cores")
                else "."
            ),
        }
        if field != "all":
            summary = phrases.get(field, "")
        else:
            summary = (
                f"{data.get('model') or 'This Mac'}, {data.get('chip')}, "
                f"{format_bytes(data.get('memory_bytes', 0))} of memory, on {os_label}."
            )
        return ToolResult(
            data=data,
            summary=summary,
            display={"kind": "facts", "title": "System", "facts": _facts(data)},
        )


class BatteryTool(Tool):
    spec = ToolSpec(
        name="get_battery",
        description="Battery percentage, power source, time remaining and health",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=80,
        examples=["What's my battery percentage?", "How's the battery holding up?"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = await self._deps.sysinfo.battery()
        if not data.get("present"):
            return ToolResult(data=data, summary="This machine has no internal battery.")
        parts = [f"Battery is at {data['percent']} percent"]
        if data.get("charging"):
            parts.append("and charging")
        elif data.get("time_remaining"):
            hours, _, minutes = data["time_remaining"].partition(":")
            parts.append(f"with about {int(hours)} hours {int(minutes)} minutes remaining")
        summary = " ".join(parts) + "."
        return ToolResult(
            data=data, summary=summary,
            display={"kind": "gauge", "title": "Battery", "value": data.get("percent"),
                     "unit": "%", "facts": _facts(data)},
        )


class StorageTool(Tool):
    spec = ToolSpec(
        name="get_storage",
        description="Free, used and total disk space",
        parameters={"type": "object", "properties": {"path": {"type": "string", "default": ""}}},
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=40,
        examples=["How much storage is left?", "Am I running out of disk space?"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = await self._deps.sysinfo.storage(args.get("path") or None)
        return ToolResult(
            data=data,
            summary=f"You have {data['free_human']} available of {data['total_human']}.",
            display={"kind": "gauge", "title": "Storage", "value": data["percent_used"],
                     "unit": "% used", "facts": [
                         ("Free", data["free_human"]), ("Used", data["used_human"]),
                         ("Total", data["total_human"])]},
        )


class MemoryTool(Tool):
    spec = ToolSpec(
        name="get_memory",
        description="Memory usage, pressure and swap",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=120,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = await self._deps.sysinfo.memory()
        summary = (
            f"{format_bytes(data['used_bytes'])} of {format_bytes(data['total_bytes'])} in use. "
            f"Memory pressure is {data['pressure']}."
        )
        return ToolResult(data=data, summary=summary,
                          display={"kind": "gauge", "title": "Memory",
                                   "value": data["percent_used"], "unit": "% used"})


class CPUTool(Tool):
    spec = ToolSpec(
        name="get_cpu",
        description="CPU load average and the busiest processes",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=200,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = await self._deps.sysinfo.cpu()
        top = data.get("processes") or []
        busiest = f" {top[0]['name']} is the busiest at {top[0]['cpu']:.0f} percent." if top else ""
        return ToolResult(
            data=data,
            summary=f"Load average is {data['load_1m']} across {data['cores']} cores.{busiest}",
            display={"kind": "table", "title": "Top processes",
                     "columns": ["Process", "CPU %", "Memory %"],
                     "rows": [[p["name"], f"{p['cpu']:.1f}", f"{p['memory']:.1f}"] for p in top]},
        )


class ProcessesTool(Tool):
    spec = ToolSpec(
        name="get_processes",
        description="List the heaviest running processes",
        parameters={
            "type": "object",
            "properties": {
                "sort": {"type": "string", "enum": ["cpu", "memory"], "default": "cpu"},
                "limit": {"type": "integer", "default": 8},
            },
        },
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=250,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rows = await self._deps.sysinfo.top_processes(int(args.get("limit", 8)),
                                                      sort=args.get("sort", "cpu"))
        if not rows:
            return ToolResult(summary="I couldn't read the process list.")
        return ToolResult(
            data={"processes": rows},
            summary=f"{rows[0]['name']} is using the most {args.get('sort', 'cpu')} "
                    f"at {rows[0][args.get('sort', 'cpu')]:.0f} percent.",
            display={"kind": "table", "title": "Processes",
                     "columns": ["PID", "Process", "CPU %", "Memory %"],
                     "rows": [[p["pid"], p["name"], f"{p['cpu']:.1f}", f"{p['memory']:.1f}"]
                              for p in rows]},
        )


class NetworkTool(Tool):
    spec = ToolSpec(
        name="get_network",
        description="Network interface, IP address, Wi-Fi network and connectivity",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=900,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = await self._deps.sysinfo.network()
        if not data.get("online"):
            return ToolResult(data=data, summary="There's no internet connection at the moment.")
        where = f" on {data['ssid']}" if data.get("ssid") else ""
        latency = f", {data['latency_ms']:.0f} milliseconds to the nearest resolver" if data.get(
            "latency_ms") else ""
        return ToolResult(data=data, summary=f"You're online{where}{latency}.",
                          display={"kind": "facts", "title": "Network", "facts": _facts(data)})


class TimeTool(Tool):
    spec = ToolSpec(
        name="get_time",
        description="Current time, date or day of the week",
        parameters={
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": ["time", "date", "both"], "default": "both"}
            },
        },
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=1,
        examples=["What time is it?", "What's today's date?"],
    )

    def __init__(self, deps=None):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        now = dt.datetime.now().astimezone()
        field = args.get("field", "both")
        time_text = now.strftime("%-I:%M %p").lower()
        date_text = now.strftime("%A, %-d %B %Y")
        summary = {
            "time": f"It's {time_text}.",
            "date": f"It's {date_text}.",
            "both": f"It's {time_text} on {date_text}.",
        }[field]
        return ToolResult(
            data={"iso": now.isoformat(), "time": time_text, "date": date_text,
                  "timezone": str(now.tzinfo), "epoch": time.time()},
            summary=summary,
        )


class DiagnosticsTool(Tool):
    spec = ToolSpec(
        name="run_diagnostics",
        description="Collect a full health snapshot of the Mac: CPU, memory, disk, battery, network, crashes",
        parameters={
            "type": "object",
            "properties": {
                "areas": {"type": "array", "default": [],
                          "description": "Subset: cpu, memory, storage, battery, network, crashes, thermal"}
            },
        },
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=3000,
        examples=["Why is my Mac slow?", "Is anything wrong with my Mac?"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        areas = args.get("areas") or None
        report = await self._deps.diagnostics.collect(
            areas, progress=lambda msg: ctx.report(msg, tool="run_diagnostics")
        )
        findings = report["findings"]
        notable = [f for f in findings if f["severity"] in {"warning", "critical"}]
        summary = report["headline"] if notable else "Everything looks healthy."
        return ToolResult(
            data=report,
            summary=summary,
            display={
                "kind": "diagnostics",
                "title": "System diagnostics",
                "severity": report["severity"],
                "findings": findings,
            },
        )


class PermissionCheckTool(Tool):
    spec = ToolSpec(
        name="check_permissions",
        description="Check which macOS privacy permissions JARVIS currently holds",
        parameters={
            "type": "object",
            "properties": {
                "kinds": {"type": "array", "default": []},
            },
        },
        risk=RiskLevel.LOW,
        category="system",
        expected_ms=2500,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        kinds = args.get("kinds") or ["accessibility", "screen_recording", "automation"]
        results: dict[str, Any] = {}
        for kind in kinds:
            granted, note = await self._deps.controller.check_permission(kind)
            results[kind] = {"granted": granted, "note": note}
        missing = [k for k, v in results.items() if not v["granted"]]
        summary = (
            "All requested permissions are in place."
            if not missing
            else f"Missing: {', '.join(m.replace('_', ' ') for m in missing)}."
        )
        return ToolResult(data=results, summary=summary,
                          display={"kind": "facts", "title": "Permissions",
                                   "facts": [(k.replace("_", " ").title(),
                                              "granted" if v["granted"] else "not granted")
                                             for k, v in results.items()]})


def _facts(data: dict[str, Any]) -> list[tuple[str, str]]:
    skip = {"raw", "processes"}
    facts: list[tuple[str, str]] = []
    for key, value in data.items():
        if key in skip or isinstance(value, (dict, list)) or value in (None, ""):
            continue
        label = key.replace("_", " ").title()
        if key.endswith("_bytes"):
            facts.append((label.replace(" Bytes", ""), format_bytes(value)))
        else:
            facts.append((label, str(value)))
    return facts[:12]


def system_tools(deps) -> list[Tool]:
    return [
        SystemInfoTool(deps),
        BatteryTool(deps),
        StorageTool(deps),
        MemoryTool(deps),
        CPUTool(deps),
        ProcessesTool(deps),
        NetworkTool(deps),
        TimeTool(deps),
        DiagnosticsTool(deps),
        PermissionCheckTool(deps),
    ]
