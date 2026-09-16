"""Deterministic system facts.

"How much storage do I have?" must never reach a language model. Every answer
here comes straight from macOS, typically in 10–80 ms, and static facts (chip,
memory size, model name) are cached for the life of the process.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import time
from typing import Any

from ..macos.controller import MacOSController


def format_bytes(value: float, unit: str = "auto") -> str:
    """Human phrasing, the way a person would say it out loud."""
    if value is None:
        return "unknown"
    step = 1000.0  # macOS reports storage in decimal units
    units = ["bytes", "KB", "MB", "GB", "TB"]
    if unit != "auto" and unit in units:
        index = units.index(unit)
        value = value / (step ** index)
        return f"{value:,.0f} {unit}"
    index = 0
    while value >= step and index < len(units) - 1:
        value /= step
        index += 1
    if index >= 3:
        return f"{value:,.1f} {units[index]}".replace(".0 ", " ")
    return f"{value:,.0f} {units[index]}"


class SystemInfo:
    def __init__(self, controller: MacOSController):
        self._c = controller
        self._static: dict[str, Any] = {}
        self._static_at = 0.0

    # ------------------------------------------------------------------
    async def overview(self) -> dict[str, Any]:
        """Machine identity: model, chip, cores, memory, OS version, uptime."""
        if self._static and time.time() - self._static_at < 3600:
            data = dict(self._static)
        else:
            data = await self._collect_static()
            self._static = dict(data)
            self._static_at = time.time()
        data["uptime"] = await self.uptime()
        data["hostname"] = platform.node()
        return data

    async def _collect_static(self) -> dict[str, Any]:
        if not self._c.is_macos:
            info: dict[str, Any] = {
                "platform": platform.system(),
                "os_version": platform.release(),
                "model": platform.machine(),
                "chip": platform.processor() or platform.machine(),
                "cores": 0,
                "memory_bytes": 0,
                "apple_silicon": False,
            }
            try:
                with open("/proc/cpuinfo", encoding="utf-8") as fh:
                    text = fh.read()
                match = re.search(r"model name\s*:\s*(.+)", text)
                if match:
                    info["chip"] = match.group(1).strip()
                info["cores"] = text.count("processor\t:") or text.count("processor :")
                with open("/proc/meminfo", encoding="utf-8") as fh:
                    mem = fh.read()
                kb = re.search(r"MemTotal:\s*(\d+)", mem)
                if kb:
                    info["memory_bytes"] = int(kb.group(1)) * 1024
            except OSError:
                pass
            return info

        version = await self._c.run(["/usr/bin/sw_vers", "-productVersion"], timeout=6.0)
        build = await self._c.run(["/usr/bin/sw_vers", "-buildVersion"], timeout=6.0)
        chip = await self._c.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], timeout=6.0)
        model_id = await self._c.run(["/usr/sbin/sysctl", "-n", "hw.model"], timeout=6.0)
        memsize = await self._c.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=6.0)
        ncpu = await self._c.run(["/usr/sbin/sysctl", "-n", "hw.ncpu"], timeout=6.0)
        perf = await self._c.run(["/usr/sbin/sysctl", "-n", "hw.perflevel0.logicalcpu"], timeout=6.0)
        eff = await self._c.run(["/usr/sbin/sysctl", "-n", "hw.perflevel1.logicalcpu"], timeout=6.0)

        chip_name = chip.stdout.strip()
        info = {
            "platform": "macOS",
            "os_version": version.stdout.strip(),
            "os_build": build.stdout.strip(),
            "os_name": _macos_name(version.stdout.strip()),
            "model_identifier": model_id.stdout.strip(),
            "chip": chip_name,
            "apple_silicon": platform.machine() == "arm64",
            "cores": _int(ncpu.stdout),
            "performance_cores": _int(perf.stdout),
            "efficiency_cores": _int(eff.stdout),
            "memory_bytes": _int(memsize.stdout),
        }
        info["memory_human"] = format_bytes(info["memory_bytes"]) if info["memory_bytes"] else ""
        marketing = await self._marketing_name()
        info["model"] = marketing or info["model_identifier"]
        return info

    async def _marketing_name(self) -> str:
        result = await self._c.run(
            ["/usr/sbin/system_profiler", "SPHardwareDataType", "-json"], timeout=25.0
        )
        if not result.ok:
            return ""
        try:
            data = json.loads(result.stdout)["SPHardwareDataType"][0]
        except (json.JSONDecodeError, KeyError, IndexError):
            return ""
        return data.get("machine_name") or data.get("model_name") or ""

    async def uptime(self) -> str:
        if self._c.is_macos:
            result = await self._c.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], timeout=6.0)
            match = re.search(r"sec\s*=\s*(\d+)", result.stdout)
            if match:
                seconds = time.time() - int(match.group(1))
                return _duration(seconds)
        try:
            with open("/proc/uptime", encoding="utf-8") as fh:
                return _duration(float(fh.read().split()[0]))
        except OSError:
            return "unknown"

    # ------------------------------------------------------------------
    async def storage(self, path: str | None = None) -> dict[str, Any]:
        target = path or ("/System/Volumes/Data" if self._c.is_macos else "/")
        usage = shutil.disk_usage(target if _exists(target) else "/")
        data = {
            "volume": target,
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
            "free_human": format_bytes(usage.free),
            "total_human": format_bytes(usage.total),
            "used_human": format_bytes(usage.used),
            "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        }
        if self._c.is_macos:
            purgeable = await self._c.run(
                ["/usr/bin/df", "-k", target], timeout=8.0
            )
            data["raw"] = purgeable.stdout.strip().splitlines()[-1] if purgeable.ok else ""
        return data

    async def battery(self) -> dict[str, Any]:
        if not self._c.is_macos:
            return {"present": False, "reason": "not macOS"}
        result = await self._c.run(["/usr/bin/pmset", "-g", "batt"], timeout=8.0)
        if not result.ok or "InternalBattery" not in result.stdout:
            return {"present": False, "reason": "no internal battery detected"}
        text = result.stdout
        percent = _int(re.search(r"(\d+)%", text).group(1)) if re.search(r"(\d+)%", text) else None
        charging = "AC Power" in text and "discharging" not in text
        remaining = None
        match = re.search(r"(\d+:\d+) remaining", text)
        if match:
            remaining = match.group(1)
        data: dict[str, Any] = {
            "present": True,
            "percent": percent,
            "charging": charging,
            "power_source": "AC Power" if "AC Power" in text else "Battery Power",
            "time_remaining": remaining,
        }
        health = await self._c.run(
            ["/usr/sbin/system_profiler", "SPPowerDataType", "-json"], timeout=25.0
        )
        if health.ok:
            try:
                power = json.loads(health.stdout)["SPPowerDataType"]
                for entry in power:
                    health_info = entry.get("sppower_battery_health_info") or {}
                    if health_info:
                        data["cycle_count"] = health_info.get("sppower_battery_cycle_count")
                        data["condition"] = health_info.get("sppower_battery_health")
                        data["max_capacity"] = health_info.get(
                            "sppower_battery_health_maximum_capacity"
                        )
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return data

    async def memory(self) -> dict[str, Any]:
        overview = await self.overview()
        total = overview.get("memory_bytes") or 0
        if not self._c.is_macos:
            values: dict[str, int] = {}
            try:
                with open("/proc/meminfo", encoding="utf-8") as fh:
                    for line in fh:
                        key, _, rest = line.partition(":")
                        values[key] = int(rest.strip().split()[0]) * 1024
            except OSError:
                pass
            available = values.get("MemAvailable", 0)
            total = values.get("MemTotal", total)
            used = max(total - available, 0)
            return {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": available,
                "percent_used": round(used / total * 100, 1) if total else 0.0,
                "pressure": "normal",
                "swap_used_bytes": max(values.get("SwapTotal", 0) - values.get("SwapFree", 0), 0),
            }

        vm = await self._c.run(["/usr/bin/vm_stat"], timeout=8.0)
        page_size = 16384 if platform.machine() == "arm64" else 4096
        stats: dict[str, int] = {}
        if vm.ok:
            size_match = re.search(r"page size of (\d+) bytes", vm.stdout)
            if size_match:
                page_size = int(size_match.group(1))
            for line in vm.stdout.splitlines()[1:]:
                key, _, value = line.partition(":")
                digits = re.sub(r"[^\d]", "", value)
                if digits:
                    stats[key.strip()] = int(digits) * page_size
        free = stats.get("Pages free", 0) + stats.get("Pages speculative", 0)
        wired = stats.get("Pages wired down", 0)
        compressed = stats.get("Pages occupied by compressor", 0)
        used = max(total - free, 0) if total else 0

        swap = await self._c.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], timeout=6.0)
        swap_used = 0.0
        match = re.search(r"used\s*=\s*([\d.]+)M", swap.stdout or "")
        if match:
            swap_used = float(match.group(1)) * 1024 * 1024

        pressure_pct = None
        pressure_cmd = await self._c.run(["/usr/bin/memory_pressure"], timeout=10.0)
        if pressure_cmd.ok:
            match = re.search(r"System-wide memory free percentage:\s*(\d+)", pressure_cmd.stdout)
            if match:
                pressure_pct = 100 - int(match.group(1))
        if pressure_pct is None and total:
            pressure_pct = round((wired + compressed) / total * 100, 1)

        level = "normal"
        if (pressure_pct or 0) > 85 or swap_used > 4 * 1024**3:
            level = "critical"
        elif (pressure_pct or 0) > 70 or swap_used > 1024**3:
            level = "elevated"

        return {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "wired_bytes": wired,
            "compressed_bytes": compressed,
            "swap_used_bytes": int(swap_used),
            "percent_used": round(used / total * 100, 1) if total else 0.0,
            "pressure_percent": pressure_pct,
            "pressure": level,
        }

    async def cpu(self) -> dict[str, Any]:
        load = (1.0, 1.0, 1.0)
        if self._c.is_macos:
            result = await self._c.run(["/usr/sbin/sysctl", "-n", "vm.loadavg"], timeout=6.0)
            nums = re.findall(r"[\d.]+", result.stdout or "")
            if len(nums) >= 3:
                load = tuple(float(n) for n in nums[:3])
        else:
            try:
                import os as _os

                load = _os.getloadavg()
            except OSError:
                pass
        overview = await self.overview()
        cores = overview.get("cores") or 1
        return {
            "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2),
            "load_15m": round(load[2], 2),
            "cores": cores,
            "load_per_core": round(load[0] / cores, 2) if cores else 0.0,
            "processes": await self.top_processes(5),
        }

    async def top_processes(self, limit: int = 8, sort: str = "cpu") -> list[dict[str, Any]]:
        key = "-%cpu" if sort == "cpu" else "-%mem"
        result = await self._c.run(
            ["/bin/ps", "-Ao", "pid,%cpu,%mem,comm", "-r" if sort == "cpu" else "-m"], timeout=10.0
        )
        if not result.ok:
            result = await self._c.run(["ps", "-eo", "pid,%cpu,%mem,comm", "--sort", key],
                                       timeout=10.0)
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                rows.append(
                    {
                        "pid": int(parts[0]),
                        "cpu": float(parts[1]),
                        "memory": float(parts[2]),
                        "name": parts[3].strip().rsplit("/", 1)[-1],
                    }
                )
            except ValueError:
                continue
        rows.sort(key=lambda r: r["cpu" if sort == "cpu" else "memory"], reverse=True)
        return rows[:limit]

    async def network(self) -> dict[str, Any]:
        data: dict[str, Any] = {"online": False, "interface": "", "ip": "", "ssid": ""}
        if self._c.is_macos:
            for iface in ("en0", "en1"):
                ip = await self._c.run(["/usr/sbin/ipconfig", "getifaddr", iface], timeout=6.0)
                if ip.ok and ip.stdout.strip():
                    data["interface"] = iface
                    data["ip"] = ip.stdout.strip()
                    break
            ssid = await self._c.run(
                ["/usr/sbin/networksetup", "-getairportnetwork", data["interface"] or "en0"],
                timeout=8.0,
            )
            if ssid.ok and ":" in ssid.stdout:
                data["ssid"] = ssid.stdout.split(":", 1)[1].strip()
        else:
            result = await self._c.run(["hostname", "-I"], timeout=6.0)
            if result.ok:
                data["ip"] = result.stdout.strip().split(" ")[0]
        ping = await self._c.run(["/sbin/ping", "-c", "1", "-t", "2", "1.1.1.1"], timeout=6.0)
        if not ping.ok:
            ping = await self._c.run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], timeout=6.0)
        data["online"] = ping.ok
        if ping.ok:
            match = re.search(r"time[=<]([\d.]+)\s*ms", ping.stdout)
            if match:
                data["latency_ms"] = float(match.group(1))
        return data

    async def thermal(self) -> dict[str, Any]:
        if not self._c.is_macos:
            return {}
        result = await self._c.run(["/usr/bin/pmset", "-g", "therm"], timeout=8.0)
        data: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                data[key.strip().lower().replace(" ", "_")] = value.strip()
        return data


def _exists(path: str) -> bool:
    from pathlib import Path

    return Path(path).exists()


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts) or "less than a minute"


_MACOS_NAMES = {
    "26": "Tahoe", "15": "Sequoia", "14": "Sonoma", "13": "Ventura",
    "12": "Monterey", "11": "Big Sur", "10.15": "Catalina",
}


def _macos_name(version: str) -> str:
    if not version:
        return ""
    major = version.split(".")[0]
    return _MACOS_NAMES.get(major, "")
