"""Live thermal/power signals from the OS.

Nothing in the Python backend can read ``ProcessInfo.thermalState`` or
``isLowPowerModeEnabled`` directly — those are Foundation APIs. The macOS
app shell (``macapp/``) observes them and reports changes over
``POST /api/system/power-state``, the one channel it has into the backend
process it spawned. Running any other way (``scripts/start.sh`` directly,
or on any OS the app shell doesn't cover) simply never calls that endpoint,
so this stays at its idle defaults and nothing throttles.

Deliberately not persisted anywhere: this is a transient condition, not a
preference, and must never overwrite what the user actually chose in
``ScreenAwarenessConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What a "serious" thermal state backs the vision-call cooldown off by —
#: turns the 8s default into ~2.7 minutes, comfortably inside the "2-5
#: minutes" range that's enough to matter without a tunable of its own.
SERIOUS_BACKOFF_MULTIPLIER = 20.0

THERMAL_STATES = ("nominal", "fair", "serious", "critical")


@dataclass
class PowerState:
    thermal_state: str = "nominal"
    low_power_mode: bool = False

    def paused(self) -> bool:
        """Skip the screen watcher's vision call outright, rather than just
        slowing it down: Low Power Mode is the user's own explicit request
        to cut power draw, and "critical" means the Mac is already
        overheating — neither is the moment to add more GPU/ANE load."""
        return self.low_power_mode or self.thermal_state == "critical"

    def vision_interval_multiplier(self) -> float:
        return SERIOUS_BACKOFF_MULTIPLIER if self.thermal_state == "serious" else 1.0

    def update(self, *, thermal_state: str | None = None, low_power_mode: bool | None = None) -> None:
        if thermal_state is not None:
            if thermal_state not in THERMAL_STATES:
                raise ValueError(f"unknown thermal state: {thermal_state!r}")
            self.thermal_state = thermal_state
        if low_power_mode is not None:
            self.low_power_mode = low_power_mode
