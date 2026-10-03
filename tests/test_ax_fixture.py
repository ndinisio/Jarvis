"""scripts/ax_fixture.py: the window that rebuilds its controls on command.

Its command handling is plain Python and tested for real here. Its AppKit half
(CocoaView) can only be run on a Mac; here it is run against stand-in modules,
which catches a typo or a wrong call order and nothing about whether AppKit
accepts the calls.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ax_fixture.py"


@pytest.fixture
def fx():
    spec = importlib.util.spec_from_file_location("ax_fixture_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingView:
    def __init__(self):
        self.calls = []

    def build(self, generation, *, saves, kind, title, distinct_ids=False):
        self.calls.append(("build", generation, saves, kind, title, distinct_ids))

    def rename(self, title):
        self.calls.append(("rename", title))

    def move(self, generation):
        self.calls.append(("move", generation))

    def quit(self):
        self.calls.append(("quit",))

    def state(self):
        return {"saves": 1}

    def probe(self):
        self.calls.append(("probe",))
        return {"active": False, "key": True}


@pytest.mark.parametrize("line, expected", [
    ("3 rebuild", (3, "rebuild", "")),
    ("12 rename Delete", (12, "rename", "Delete")),
    ("5 twin", (5, "twin", "")),
    ("  7   rename   two words here ", (7, "rename", "two words here")),
    ("rebuild", None),                       # no number
    ("x rebuild", None),
    ("4 explode", None),                     # not a command
    ("", None),
])
def test_commands_are_parsed_strictly(fx, line, expected):
    assert fx.parse_command(line) == expected


def test_each_command_builds_what_it_says_and_counts_a_generation(fx):
    view = RecordingView()
    fixture = fx.Fixture(view)
    assert fixture.apply("restore") == {"generation": 1, "saves": 1}
    fixture.apply("rebuild")
    fixture.apply("duplicate")
    fixture.apply("twin")
    fixture.apply("impostor")
    fixture.apply("rename", "Delete")
    fixture.apply("move")
    fixture.apply("quit")
    assert view.calls == [
        ("build", 1, 1, "button", "Save", False),
        ("build", 2, 1, "button", "Save", False),
        ("build", 3, 2, "button", "Save", False),
        ("build", 4, 2, "button", "Save", True),
        ("build", 5, 1, "checkbox", "Save", False),
        ("rename", "Delete"),
        ("move", 7),
        ("quit",),
    ]
    assert fixture.generation == 8


def test_the_command_file_runs_new_lines_once_and_acknowledges_each(fx, tmp_path):
    log = []
    path = tmp_path / "commands"
    path.write_text("")
    commands = fx.CommandFile(path, fx.Fixture(RecordingView()), log.append)
    commands.poll()
    assert log == []
    path.write_text("1 restore\n2 rebuild\n")
    commands.poll()
    commands.poll()
    assert [line.split(":")[:2] for line in log] == [["ack", "1"], ["ack", "2"]]
    assert json.loads(log[1].split(":", 2)[2]) == {"generation": 2, "saves": 1}
    path.write_text("1 restore\n2 rebuild\n3 rename Delete\n")
    commands.poll()
    assert len(log) == 3, "only the new line ran"


def test_a_bad_line_or_a_failing_command_is_reported_and_the_next_one_still_runs(fx, tmp_path):
    class Exploding(RecordingView):
        def rename(self, title):
            raise RuntimeError("no such control")

    log = []
    path = tmp_path / "commands"
    path.write_text("nonsense\n1 rename X\n2 restore\n")
    fx.CommandFile(path, fx.Fixture(Exploding()), log.append).poll()
    assert log[0].startswith("error:0:not a command")
    assert log[1] == "error:1:RuntimeError: no such control"
    assert log[2].startswith("ack:2:")


def test_a_missing_command_file_is_not_an_error(fx, tmp_path):
    log = []
    fx.CommandFile(tmp_path / "nope", fx.Fixture(RecordingView()), log.append).poll()
    assert log == []


def test_a_probe_answers_the_apps_own_state_and_changes_nothing(fx, tmp_path):
    """So a check can ask the app, not Accessibility, whether it is active - without a probe counting as a
    build: the generation presses are matched against must not move."""
    view = RecordingView()
    fixture = fx.Fixture(view)
    first = fixture.apply("restore")
    probed = fixture.apply("probe")
    assert probed == {"generation": first["generation"], "active": False, "key": True}
    assert fixture.apply("probe")["generation"] == first["generation"], "asking twice moves nothing"
    assert fixture.apply("rebuild")["generation"] == first["generation"] + 1
    assert [call[0] for call in view.calls] == ["build", "probe", "probe", "build"]
    log, path = [], tmp_path / "commands"
    path.write_text("1 probe\n")
    fx.CommandFile(path, fixture, log.append).poll()
    assert log[0].startswith("ack:1:") and json.loads(log[0].split(":", 2)[2])["active"] is False


def test_the_log_appends_lines(fx, tmp_path):
    log = fx.Log(tmp_path / "log")
    log("one")
    log("two")
    assert (tmp_path / "log").read_text() == "one\ntwo\n"


# --- the AppKit half, against stand-ins -----------------------------------------------------
class FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


def stand_in_modules():
    appkit, foundation = MagicMock(), MagicMock()
    foundation.NSObject = FakeNSObject
    return appkit, foundation


def test_the_window_builds_rebuilds_renames_moves_and_quits_without_a_slip(fx):
    appkit, foundation = stand_in_modules()
    log = []
    view = fx.CocoaView(appkit, foundation, log.append)
    assert view.state() == {"saves": 1, "other_window": False}
    view.build(1, saves=2, kind="button", title="Save")
    assert view.state()["saves"] == 2
    view.build(1, saves=2, kind="button", title="Save", distinct_ids=True)
    view.build(2, saves=1, kind="checkbox", title="Save")
    view.rename("Delete")
    view.move(3)
    assert view.state() == {"saves": 1, "other_window": True}
    view.build(4, saves=1, kind="button", title="Save")
    assert view.state() == {"saves": 1, "other_window": False}, "a rebuild closes the other window"
    view.quit()
    appkit.NSApp.terminate_.assert_called_once_with(None)


def test_a_press_is_logged_with_the_build_of_the_control_that_was_pressed(fx):
    appkit, foundation = stand_in_modules()
    log = []
    view = fx.CocoaView(appkit, foundation, log.append)
    sender = MagicMock()
    sender.title.return_value, sender.identifier.return_value, sender.tag.return_value = "Save", "save", 5
    view.target.clicked_(sender)
    view.target.toggled_(sender)
    assert [line for line in log if line.startswith(("click:", "toggle:"))] == [
        "click:Save:save:born=5", "toggle:Save:save:born=5"]
    polled = []
    view.start(lambda: polled.append(1))
    view.target.tick_(None)
    assert polled == [1]
    foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.assert_called_once()


def _handler(line: str) -> dict[str, str]:
    kind, *fields = line.split(":")
    return dict(field.split("=", 1) for field in fields if "=" in field)


def test_each_press_is_preceded_by_whether_the_app_was_already_active(fx):
    """So a check of background presses can tell activation that came *before* the fixture's own
    code from activation that came after it. The fixture's action only ever writes lines."""
    appkit, foundation = stand_in_modules()
    log = []
    view = fx.CocoaView(appkit, foundation, log.append)
    application = appkit.NSApplication.sharedApplication.return_value
    application.isActive.return_value, application.currentEvent.return_value = False, None
    view.main.isKeyWindow.return_value, view.main.isMainWindow.return_value = True, False
    sender = MagicMock()
    sender.title.return_value, sender.identifier.return_value, sender.tag.return_value = "Save", "save", 5
    view.target.clicked_(sender)
    assert log[-2].startswith("handler:click:") and log[-1] == "click:Save:save:born=5", "the state first"
    fields = _handler(log[-2])
    assert (fields["active"], fields["key"], fields["main"], fields["event"]) == ("0", "1", "0", "none")
    assert float(fields["t"]) > 0
    application.isActive.return_value = True
    application.currentEvent.return_value = MagicMock(**{"type.return_value": 1})
    view.target.toggled_(sender)
    assert _handler(log[-2])["active"] == "1" and _handler(log[-2])["event"] == "1"
    assert log[-2].startswith("handler:toggle:")


def test_a_fixture_that_cannot_read_its_state_says_so_and_still_logs_the_press(fx):
    appkit, foundation = stand_in_modules()
    log = []
    view = fx.CocoaView(appkit, foundation, log.append)
    appkit.NSApplication.sharedApplication.side_effect = RuntimeError("no application")
    sender = MagicMock()
    sender.title.return_value, sender.identifier.return_value, sender.tag.return_value = "Save", "save", 1
    view.target.clicked_(sender)
    assert log[-2].startswith("handler:click:unavailable=RuntimeError:t=") and log[-1] == "click:Save:save:born=1"


def test_the_app_logs_when_it_becomes_active_and_when_it_stops_being(fx):
    appkit, foundation = stand_in_modules()
    log = []
    view = fx.CocoaView(appkit, foundation, log.append)
    centre = foundation.NSNotificationCenter.defaultCenter.return_value
    registered = {call.args[1]: call.args[2] for call in centre.addObserver_selector_name_object_.call_args_list}
    assert registered == {"activated:": appkit.NSApplicationDidBecomeActiveNotification,
                          "resigned:": appkit.NSApplicationDidResignActiveNotification}
    view.target.activated_(None)
    view.target.resigned_(None)
    assert [line.split(":t=")[0] for line in log[-2:]] == ["activation:became", "activation:resigned"]
    assert float(log[-1].split(":t=")[1]) >= float(log[-2].split(":t=")[1]), "one monotonic clock"


def test_the_cocoa_probe_reports_appkits_active_flag_and_the_key_window_and_never_raises(fx):
    appkit, foundation = stand_in_modules()
    view = fx.CocoaView(appkit, foundation, [].append)
    application = appkit.NSApplication.sharedApplication.return_value
    application.isActive.return_value = False
    view.main.isKeyWindow.return_value = True
    assert view.probe() == {"active": False, "key": True}
    application.isActive.return_value = True
    assert view.probe()["active"] is True
    appkit.NSApplication.sharedApplication.side_effect = RuntimeError("no application")
    assert view.probe() == {"active": None, "key": None, "why": "RuntimeError"}


def test_a_notification_centre_that_refuses_is_noted_and_does_not_stop_the_window(fx):
    appkit, foundation = stand_in_modules()
    foundation.NSNotificationCenter.defaultCenter.side_effect = RuntimeError("no centre")
    log = []
    view = fx.CocoaView(appkit, foundation, log.append)
    assert "note:no activation log: RuntimeError" in log and view.state()["saves"] == 1


def test_the_inert_button_has_nothing_to_run_when_it_is_pressed(fx):
    """A button with no target and no action: pressing it by Accessibility runs none of the fixture's code."""
    appkit, foundation = stand_in_modules()
    buttons = []

    def new_button():
        button = MagicMock()
        buttons.append(button)
        return button

    appkit.NSButton.alloc.return_value.initWithFrame_.side_effect = lambda frame: new_button()
    fx.CocoaView(appkit, foundation, [].append)
    inert = next(b for b in buttons if b.setIdentifier_.call_args == (("inert",),))
    inert.setTitle_.assert_called_once_with("Inert")
    inert.setTarget_.assert_not_called()
    inert.setAction_.assert_not_called()
    assert all(b.setAction_.called for b in buttons if b is not inert and b.setIdentifier_.call_args
               and b.setIdentifier_.call_args[0][0] in {"cancel", "save"})


def test_running_it_announces_itself_and_starts_the_app(fx, tmp_path, monkeypatch):
    appkit, foundation = stand_in_modules()
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    log, commands = tmp_path / "log", tmp_path / "commands"
    assert fx.run(["--log", str(log), "--commands", str(commands)]) == 0
    assert log.read_text().startswith("ready:")
    appkit.NSApplication.sharedApplication.return_value.run.assert_called_once()
