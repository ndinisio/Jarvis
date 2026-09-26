"""PowerState: the thermal/Low Power Mode signal the macOS app shell reports
(see backend/jarvis/core/power.py) — never persisted, purely in-memory."""

from __future__ import annotations

import pytest
from jarvis.core.power import PowerState


def test_defaults_to_unthrottled():
    state = PowerState()
    assert state.paused() is False
    assert state.vision_interval_multiplier() == 1.0


def test_low_power_mode_pauses_outright():
    state = PowerState()
    state.update(low_power_mode=True)
    assert state.paused() is True


def test_critical_thermal_state_pauses_outright():
    state = PowerState()
    state.update(thermal_state="critical")
    assert state.paused() is True


def test_serious_thermal_state_backs_off_but_does_not_pause():
    state = PowerState()
    state.update(thermal_state="serious")
    assert state.paused() is False
    assert state.vision_interval_multiplier() > 1.0


def test_fair_thermal_state_is_unthrottled():
    state = PowerState()
    state.update(thermal_state="fair")
    assert state.paused() is False
    assert state.vision_interval_multiplier() == 1.0


def test_an_unknown_thermal_state_is_rejected_not_silently_accepted():
    state = PowerState()
    with pytest.raises(ValueError):
        state.update(thermal_state="melting")
    assert state.thermal_state == "nominal", "a rejected update must not partially apply"


def test_update_only_touches_the_field_it_is_given():
    state = PowerState()
    state.update(low_power_mode=True)
    state.update(thermal_state="serious")
    assert state.low_power_mode is True, "setting thermal_state must not reset low_power_mode"
