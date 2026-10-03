"""The suite's host is never the machine it runs on (see conftest._hermetic_host)."""

from __future__ import annotations

import platform

from jarvis.surfaces.native import NativeSurface
from jarvis.tools import registry
from jarvis.tools.macos import controller as controller_module


async def test_every_test_sees_a_host_that_is_not_a_mac(app):
    assert platform.system() == "Linux"
    assert registry.IS_MACOS is False and controller_module.IS_MACOS is False
    assert app.controller.is_macos is False
    assert NativeSurface().available() is False


async def test_a_test_that_wants_mac_behaviour_can_still_ask_for_it(app, monkeypatch):
    monkeypatch.setattr(app.controller, "is_macos", True)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert app.controller.is_macos is True and platform.system() == "Darwin"


async def test_tools_that_drive_a_mac_are_refused_here_rather_than_run(app, ctx):
    clicked = await app.deps.registry.call("click_element", {"label": "Search"}, ctx)
    assert clicked.ok is False and "only works on macOS" in clicked.summary
    launched = await app.deps.registry.call("open_application", {"name": "Calculator"}, ctx)
    assert launched.ok is False, "nothing was opened on the machine running the tests"
