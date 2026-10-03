#!/usr/bin/env python3
"""A small AppKit window whose controls can be rebuilt on command, for checking
JARVIS's native control on a real Mac.

Real apps can't be made to produce the cases that matter most for re-finding a
stale control — *this* button rebuilt, *two* identical buttons, a look-alike of
another kind, a name changed in place, the same name in another window — and
they change between macOS releases. This window can, every time, and it records
what was actually clicked in a log of its own, so a check can verify an action
against the app's own account of it rather than against the Accessibility tree
that is under test.

    scripts/check_native.py --stale     # starts this, drives it, stops it

Run on its own it is a window and a command file:

    python scripts/ax_fixture.py --log /tmp/fixture.log --commands /tmp/fixture.cmd
    echo "1 rebuild" >> /tmp/fixture.cmd     # an "ack:1:{…}" line appears in the log

Commands, one per line, ``<number> <name> [argument]``:

    restore          one Save button, as at the start
    rebuild          throw the Save button away and make a new one, same name
    duplicate        rebuild it, and add a second Save beside it, identical in every way
    twin             the same, but the second Save has an identifier of its own
    impostor         replace it with a checkbox that is also called Save
    rename <name>    change the Save button's name, in place
    move             take Save out of this window and put it in another
    quit

Log lines: ``ready:<pid>``, ``ack:<number>:<json state>``, ``error:<number>:<why>``,
and for every press ``click:<name>:<identifier>:born=<generation>`` (a button) or
``toggle:…`` (a checkbox). ``born`` says which build of the control was pressed.

Two kinds of line exist only to say *when the app became active*, which a check of background
presses needs to attribute (the fixture's action itself only writes a line; it never activates,
orders a window or asks for focus): ``handler:<click|toggle>:active=<0|1>:key=<0|1>:main=<0|1>:
event=<none|type>:t=<seconds>`` just before a press is logged - whether the app was already active
when its action ran - and ``activation:<became|resigned>:t=<seconds>`` whenever it changes. ``t`` is
``time.monotonic()``, the same clock in every process on the machine. The window also has an
``Inert`` button with no action, to press without any fixture code running at all.

Needs the ``native`` extra (PyObjC). The command and log handling is plain
Python and tested in CI; the AppKit half is only checked there for typos, and
runs for real only on a Mac.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

TITLE = "JARVIS Fixture"
OTHER_TITLE = "JARVIS Fixture (other)"
COMMANDS = ("restore", "rebuild", "duplicate", "twin", "impostor", "rename", "move", "quit")


def parse_command(line: str) -> tuple[int, str, str] | None:
    """``"3 rename Delete"`` → ``(3, "rename", "Delete")``; None if it isn't one."""
    parts = line.strip().split(None, 2)
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in COMMANDS:
        return None
    return int(parts[0]), parts[1], parts[2] if len(parts) > 2 else ""


class Fixture:
    """Applies commands to a view and says what the result is. The view is
    whatever draws the window (``CocoaView``, or a stand-in in tests):
    ``build(generation, saves, kind, title, distinct_ids)``, ``rename(title)``,
    ``move(generation)``, ``quit()`` and ``state()``."""

    def __init__(self, view: Any):
        self.view = view
        self.generation = 0

    def apply(self, name: str, argument: str = "") -> dict[str, Any]:
        self.generation += 1
        if name in {"restore", "rebuild"}:
            self.view.build(self.generation, saves=1, kind="button", title="Save")
        elif name in {"duplicate", "twin"}:
            self.view.build(self.generation, saves=2, kind="button", title="Save",
                            distinct_ids=name == "twin")
        elif name == "impostor":
            self.view.build(self.generation, saves=1, kind="checkbox", title="Save")
        elif name == "rename":
            self.view.rename(argument or "Renamed")
        elif name == "move":
            self.view.move(self.generation)
        elif name == "quit":
            self.view.quit()
        else:
            raise ValueError(f"no command “{name}”")
        return {"generation": self.generation, **self.view.state()}


class CommandFile:
    """Runs the lines added to a command file since it last looked, and
    acknowledges each in the log. A bad line is reported, never fatal."""

    def __init__(self, path: Path, fixture: Fixture, log: Callable[[str], None]):
        self.path, self.fixture, self.log = Path(path), fixture, log
        self.done = 0

    def poll(self) -> None:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        pending, self.done = lines[self.done:], len(lines)
        for line in pending:
            if not line.strip():
                continue
            command = parse_command(line)
            if command is None:
                self.log(f"error:0:not a command: {line.strip()[:80]}")
                continue
            number, name, argument = command
            try:
                state = self.fixture.apply(name, argument)
            except Exception as exc:
                self.log(f"error:{number}:{type(exc).__name__}: {exc}")
                continue
            self.log(f"ack:{number}:{json.dumps(state, sort_keys=True)}")


class Log:
    def __init__(self, path: Path):
        self.path = Path(path)

    def __call__(self, line: str) -> None:
        with self.path.open("a") as handle:
            handle.write(line + "\n")


class CocoaView:
    """The window itself. Takes the AppKit and Foundation modules as arguments
    so the CI smoke test can run it against stand-ins."""

    #: NSWindowStyleMask titled | closable | miniaturizable; NSBackingStoreBuffered.
    _STYLE, _BUFFERED = 1 | 2 | 4, 2
    _ROUNDED, _SWITCH = 1, 3

    def __init__(self, AppKit: Any, Foundation: Any, log: Callable[[str], None]):
        self.AK, self.F, self.log = AppKit, Foundation, log
        self.target = self._make_target()
        self.main = self._window(TITLE, (80, 300))
        self.other: Any = None
        self.saves: list[Any] = []
        self.tick: Callable[[], None] = lambda: None
        self._fill_main()
        self.build(0, saves=1, kind="button", title="Save")
        self.main.makeKeyAndOrderFront_(None)
        self._watch_activation()

    # -- the commands' effects --------------------------------------------------------------
    def build(self, generation: int, *, saves: int, kind: str, title: str,
              distinct_ids: bool = False) -> None:
        for control in self.saves:
            control.removeFromSuperview()
        self.saves = []
        if self.other is not None:
            self.other.close()
            self.other = None
        for index in range(saves):
            identifier = f"save{index + 1}" if distinct_ids and index else "save"
            control = self._button(title, identifier, (20 + 100 * index, 240, 90, 28), generation, kind)
            self.main.contentView().addSubview_(control)
            self.saves.append(control)

    def rename(self, title: str) -> None:
        for control in self.saves:
            control.setTitle_(title)

    def move(self, generation: int) -> None:
        for control in self.saves:
            control.removeFromSuperview()
        self.saves = []
        self.other = self._window(OTHER_TITLE, (560, 300))
        moved = self._button("Save", "save", (20, 240, 90, 28), generation, "button")
        self.other.contentView().addSubview_(moved)
        self.other.makeKeyAndOrderFront_(None)
        self.saves = [moved]

    def state(self) -> dict[str, Any]:
        return {"saves": len(self.saves), "other_window": self.other is not None}

    def quit(self) -> None:
        self.AK.NSApp.terminate_(None)

    # -- AppKit -----------------------------------------------------------------------------
    def start(self, poll: Callable[[], None], interval_s: float = 0.05) -> None:
        self.tick = poll
        self.F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval_s, self.target, "tick:", None, True)

    def state_line(self, kind: str) -> str:
        """The ``handler:`` line: was the app already active, and was its window key, when this
        control's action started to run? Never raises - a fixture that can't say says so."""
        try:
            app = self.AK.NSApplication.sharedApplication()
            event = app.currentEvent()
            return (f"handler:{kind}:active={int(bool(app.isActive()))}:key={int(bool(self.main.isKeyWindow()))}:"
                    f"main={int(bool(self.main.isMainWindow()))}:"
                    f"event={event.type() if event is not None else 'none'}:t={time.monotonic():.4f}")
        except Exception as exc:
            return f"handler:{kind}:unavailable={type(exc).__name__}:t={time.monotonic():.4f}"

    def _watch_activation(self) -> None:
        """Log when the app becomes active or stops being (see the module docstring). Best effort."""
        try:
            centre = self.F.NSNotificationCenter.defaultCenter()
            centre.addObserver_selector_name_object_(self.target, "activated:",
                                                     self.AK.NSApplicationDidBecomeActiveNotification, None)
            centre.addObserver_selector_name_object_(self.target, "resigned:",
                                                     self.AK.NSApplicationDidResignActiveNotification, None)
        except Exception as exc:
            self.log(f"note:no activation log: {type(exc).__name__}")

    def _make_target(self) -> Any:
        view = self

        class Target(self.F.NSObject):
            def clicked_(self, sender):
                view.log(view.state_line("click"))
                view.log(f"click:{sender.title()}:{sender.identifier()}:born={sender.tag()}")

            def toggled_(self, sender):
                view.log(view.state_line("toggle"))
                view.log(f"toggle:{sender.title()}:{sender.identifier()}:born={sender.tag()}")

            def activated_(self, _note):
                view.log(f"activation:became:t={time.monotonic():.4f}")

            def resigned_(self, _note):
                view.log(f"activation:resigned:t={time.monotonic():.4f}")

            def tick_(self, _timer):
                view.tick()

        return Target.alloc().init()

    def _window(self, title: str, origin: tuple[int, int]) -> Any:
        window = self.AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            (origin, (420, 300)), self._STYLE, self._BUFFERED, False)
        window.setTitle_(title)
        window.setReleasedWhenClosed_(False)
        return window

    def _button(self, title: str, identifier: str, frame: tuple, born: int, kind: str) -> Any:
        control = self.AK.NSButton.alloc().initWithFrame_(((frame[0], frame[1]), (frame[2], frame[3])))
        control.setTitle_(title)
        if kind == "checkbox":
            control.setButtonType_(self._SWITCH)
            control.setFrame_(((frame[0], frame[1]), (140, 22)))
        else:
            control.setBezelStyle_(self._ROUNDED)
        control.setIdentifier_(identifier)
        control.setTag_(born)
        control.setTarget_(self.target)
        control.setAction_("toggled:" if kind == "checkbox" else "clicked:")
        return control

    def _fill_main(self) -> None:
        AK, content = self.AK, self.main.contentView()
        cancel = AK.NSButton.alloc().initWithFrame_(((120, 240), (90, 28)))
        cancel.setTitle_("Cancel")
        cancel.setBezelStyle_(self._ROUNDED)
        cancel.setIdentifier_("cancel")
        cancel.setTarget_(self.target)
        cancel.setAction_("clicked:")
        remember = AK.NSButton.alloc().initWithFrame_(((20, 200), (200, 22)))
        remember.setButtonType_(self._SWITCH)
        remember.setTitle_("Remember me")
        remember.setIdentifier_("remember")
        name = AK.NSTextField.alloc().initWithFrame_(((20, 160), (200, 24)))
        name.setIdentifier_("name")
        name.setPlaceholderString_("Name")
        fmt = AK.NSPopUpButton.alloc().initWithFrame_pullsDown_(((20, 120), (200, 26)), False)
        fmt.addItemsWithTitles_(["PDF", "Plain Text", "Rich Text"])
        fmt.setIdentifier_("format")
        inert = AK.NSButton.alloc().initWithFrame_(((320, 240), (90, 28)))   # no target, no action
        inert.setTitle_("Inert")
        inert.setBezelStyle_(self._ROUNDED)
        inert.setIdentifier_("inert")
        for control in (cancel, remember, name, fmt, inert):
            content.addSubview_(control)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--commands", required=True, type=Path)
    args = parser.parse_args(argv)
    import AppKit
    import Foundation

    log = Log(args.log)
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(0)                      # an ordinary app: a window, in the Dock
    view = CocoaView(AppKit, Foundation, log)
    commands = CommandFile(args.commands, Fixture(view), log)
    view.start(commands.poll)
    app.activateIgnoringOtherApps_(True)
    log(f"ready:{os.getpid()}")
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(run())
