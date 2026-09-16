"""Mac diagnostics.

"Why is my Mac slow?" is answered by *measuring first*. This module collects
evidence and turns it into structured observations; the language model is only
brought in afterwards to explain and advise, and it is given the findings
rather than asked to invent them.

Every finding carries a severity and is tagged OBSERVED. Causes and
recommendations are produced separately so the UI (and the spoken answer) can
keep the distinction the user needs:

    OBSERVED            measured facts
    LIKELY CAUSE        inference from those facts
    RECOMMENDED ACTION  what to do, destructive steps gated behind confirmation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..macos.controller import MacOSController
from .info import SystemInfo, format_bytes


@dataclass(slots=True)
class Finding:
    #: "ok" | "info" | "warning" | "critical"
    severity: str
    area: str
    observation: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "area": self.area,
            "observation": self.observation,
            "detail": self.detail,
        }


SEVERITY_RANK = {"ok": 0, "info": 1, "warning": 2, "critical": 3}


class Diagnostics:
    def __init__(self, controller: MacOSController, info: SystemInfo):
        self._c = controller
        self._info = info

    async def collect(self, areas: list[str] | None = None,
                      progress=None) -> dict[str, Any]:
        areas = areas or ["cpu", "memory", "storage", "battery", "network", "processes", "crashes"]
        findings: list[Finding] = []
        raw: dict[str, Any] = {}

        async def step(name: str, coro):
            if progress:
                progress(f"Checking {name}…")
            return await coro

        if "memory" in areas:
            raw["memory"] = await step("memory pressure", self._info.memory())
            findings += self._memory_findings(raw["memory"])
        if "cpu" in areas or "processes" in areas:
            raw["cpu"] = await step("CPU load", self._info.cpu())
            raw["top_cpu"] = await self._info.top_processes(6, sort="cpu")
            raw["top_memory"] = await self._info.top_processes(6, sort="memory")
            findings += self._cpu_findings(raw["cpu"], raw["top_cpu"], raw["top_memory"])
        if "storage" in areas:
            raw["storage"] = await step("disk space", self._info.storage())
            findings += self._storage_findings(raw["storage"])
        if "battery" in areas:
            raw["battery"] = await step("battery health", self._info.battery())
            findings += self._battery_findings(raw["battery"])
        if "network" in areas:
            raw["network"] = await step("network", self._info.network())
            findings += self._network_findings(raw["network"])
        if "crashes" in areas:
            raw["crashes"] = await step("recent crash reports", self.recent_crashes())
            findings += self._crash_findings(raw["crashes"])
        if "thermal" in areas or "cpu" in areas:
            thermal = await self._info.thermal()
            if thermal:
                raw["thermal"] = thermal
                findings += self._thermal_findings(thermal)

        raw["overview"] = await self._info.overview()
        findings.sort(key=lambda f: SEVERITY_RANK.get(f.severity, 0), reverse=True)
        worst = findings[0].severity if findings else "ok"
        return {
            "findings": [f.as_dict() for f in findings],
            "raw": raw,
            "severity": worst,
            "headline": _headline(findings),
        }

    async def recent_crashes(self, limit: int = 8) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        directories = [
            Path.home() / "Library/Logs/DiagnosticReports",
            Path("/Library/Logs/DiagnosticReports"),
        ]
        for directory in directories:
            try:
                entries = sorted(
                    directory.glob("*.ips"), key=lambda p: p.stat().st_mtime, reverse=True
                )
            except OSError:
                continue
            for entry in entries[:limit]:
                reports.append(
                    {
                        "name": entry.name.split("-")[0],
                        "file": str(entry),
                        "when": entry.stat().st_mtime,
                    }
                )
        reports.sort(key=lambda r: r["when"], reverse=True)
        return reports[:limit]

    # -- finding builders --------------------------------------------------
    @staticmethod
    def _memory_findings(memory: dict[str, Any]) -> list[Finding]:
        out: list[Finding] = []
        pressure = memory.get("pressure", "normal")
        swap = memory.get("swap_used_bytes", 0)
        if pressure == "critical":
            out.append(Finding("critical", "memory",
                               f"Memory pressure is critical ({memory.get('pressure_percent')}%).",
                               memory))
        elif pressure == "elevated":
            out.append(Finding("warning", "memory",
                               f"Memory pressure is elevated ({memory.get('pressure_percent')}%).",
                               memory))
        else:
            out.append(Finding("ok", "memory", "Memory pressure is normal.", memory))
        if swap > 2 * 1024**3:
            out.append(Finding("warning", "memory",
                               f"{format_bytes(swap)} of swap is in use — the system is paging to disk.",
                               {"swap_used_bytes": swap}))
        return out

    @staticmethod
    def _cpu_findings(cpu: dict[str, Any], top_cpu: list[dict], top_mem: list[dict]) -> list[Finding]:
        out: list[Finding] = []
        per_core = cpu.get("load_per_core", 0)
        if per_core > 1.5:
            out.append(Finding("critical", "cpu",
                               f"CPU load is {cpu.get('load_1m')} across {cpu.get('cores')} cores "
                               "— the machine is saturated.", cpu))
        elif per_core > 0.8:
            out.append(Finding("warning", "cpu",
                               f"CPU load is high ({cpu.get('load_1m')} over {cpu.get('cores')} cores).",
                               cpu))
        else:
            out.append(Finding("ok", "cpu", f"CPU load is normal ({cpu.get('load_1m')}).", cpu))
        for proc in top_cpu[:2]:
            if proc.get("cpu", 0) > 80:
                out.append(Finding("warning", "process",
                                   f"{proc['name']} is using {proc['cpu']:.0f}% CPU.", proc))
        for proc in top_mem[:2]:
            if proc.get("memory", 0) > 25:
                out.append(Finding("warning", "process",
                                   f"{proc['name']} is using {proc['memory']:.0f}% of memory.", proc))
        return out

    @staticmethod
    def _storage_findings(storage: dict[str, Any]) -> list[Finding]:
        free = storage.get("free_bytes", 0)
        percent = storage.get("percent_used", 0)
        if free < 10 * 1000**3 or percent > 95:
            return [Finding("critical", "storage",
                            f"Only {storage.get('free_human')} of disk space remains "
                            f"({percent}% used).", storage)]
        if free < 25 * 1000**3 or percent > 88:
            return [Finding("warning", "storage",
                            f"Disk space is getting tight: {storage.get('free_human')} free.",
                            storage)]
        return [Finding("ok", "storage", f"{storage.get('free_human')} of disk space is free.",
                        storage)]

    @staticmethod
    def _battery_findings(battery: dict[str, Any]) -> list[Finding]:
        if not battery.get("present"):
            return []
        out: list[Finding] = []
        condition = (battery.get("condition") or "").lower()
        if condition and condition not in {"normal", "good"}:
            out.append(Finding("warning", "battery",
                               f"Battery condition reports as {battery.get('condition')}.", battery))
        cycles = battery.get("cycle_count")
        if isinstance(cycles, int) and cycles > 900:
            out.append(Finding("info", "battery", f"Battery has {cycles} charge cycles.", battery))
        percent = battery.get("percent")
        if isinstance(percent, int) and percent < 15 and not battery.get("charging"):
            out.append(Finding("warning", "battery", f"Battery is at {percent}% and discharging.",
                               battery))
        if not out:
            out.append(Finding("ok", "battery",
                               f"Battery is at {battery.get('percent')}% and in normal condition.",
                               battery))
        return out

    @staticmethod
    def _network_findings(network: dict[str, Any]) -> list[Finding]:
        if not network.get("online"):
            return [Finding("warning", "network", "No internet connectivity was detected.", network)]
        latency = network.get("latency_ms")
        if latency and latency > 150:
            return [Finding("warning", "network", f"Network latency is high ({latency:.0f} ms).",
                            network)]
        return [Finding("ok", "network",
                        "Network is reachable" + (f" ({latency:.0f} ms)." if latency else "."),
                        network)]

    @staticmethod
    def _crash_findings(crashes: list[dict[str, Any]]) -> list[Finding]:
        import time

        recent = [c for c in crashes if time.time() - c["when"] < 86400 * 3]
        if not recent:
            return []
        names: dict[str, int] = {}
        for crash in recent:
            names[crash["name"]] = names.get(crash["name"], 0) + 1
        worst = max(names.items(), key=lambda kv: kv[1])
        severity = "warning" if worst[1] > 2 else "info"
        return [Finding(severity, "crashes",
                        f"{len(recent)} crash report(s) in the last three days; "
                        f"{worst[0]} accounts for {worst[1]}.", {"counts": names})]

    @staticmethod
    def _thermal_findings(thermal: dict[str, Any]) -> list[Finding]:
        out: list[Finding] = []
        for key, value in thermal.items():
            if "speed_limit" in key:
                try:
                    limit = int(str(value).strip())
                except ValueError:
                    continue
                if limit < 100:
                    out.append(Finding("warning", "thermal",
                                       f"CPU speed is limited to {limit}% — thermal or power throttling.",
                                       thermal))
        return out


def _headline(findings: list[Finding]) -> str:
    critical = [f for f in findings if f.severity == "critical"]
    warnings = [f for f in findings if f.severity == "warning"]
    if critical:
        return critical[0].observation
    if warnings:
        return warnings[0].observation
    return "Everything looks healthy."
