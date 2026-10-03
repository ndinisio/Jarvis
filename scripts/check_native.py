#!/usr/bin/env python3
"""Check JARVIS's Mac app control on this Mac, for real.

The native surface (backend/jarvis/surfaces/native/) is tested in CI against
a fake accessibility tree; this runs the real thing. By default it only
looks: it lists the front window of an app, its menus, and reads text off a
screenshot of it. With --act it also opens TextEdit, types into a new
document, makes it bold from the Format menu, reads it back, and closes it
without saving. With --controls it goes through the rest of what JARVIS does
to a window — clicking buttons (click_control), choosing from a pop-up
(choose_option), dragging a file onto a folder (drag_control), and clicking
what a screenshot shows (mark_screen / click_mark) — in Calculator, TextEdit's
Save sheet and a throwaway folder on the Desktop.

    .venv/bin/python scripts/check_native.py            # look at the frontmost app
    .venv/bin/python scripts/check_native.py --app Notes
    .venv/bin/python scripts/check_native.py --act      # the TextEdit round trip
    .venv/bin/python scripts/check_native.py --controls # click, pop-up, drag, marks
    .venv/bin/python scripts/check_native.py --stale    # re-finding a rebuilt control, for real
    .venv/bin/python scripts/check_native.py --observe  # do AXObserver notifications fire?

--stale opens a small window of its own (scripts/ax_fixture.py) whose button can
be rebuilt, duplicated, replaced by a look-alike, renamed or moved on command,
and checks JARVIS re-finds the one it should and refuses every other case —
against the window's own log of what was clicked. ⚠ means the check couldn't
tell (macOS kept the old reference valid, so there was nothing to re-find).

--observe is the check for the optional Accessibility observer thread
(automation.native_observer): whether macOS posts the notifications JARVIS
would wake on, how much sooner than polling, what happens when a subscription
can't be made, and that the thread leaves the event loop alone. Run it before
turning that setting on.

find_on_screen is the one native tool not exercised here: it asks a vision
model which mark is which, so it needs a configured model (evals/run_mac.py
drives it end to end). --controls does check the marks and overlay it picks from.

Needs the native extras (pip install -e '.[native]') and, for the process
running it (Terminal), Accessibility and — for the screenshot — Screen
Recording, in System Settings → Privacy & Security.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import native_validation as nv  # noqa: E402  (this folder; see its docstring)
from jarvis.surfaces.native import AmbiguousOption, NativeError, NativeSurface  # noqa: E402
from jarvis.surfaces.native.input import resolve_key  # noqa: E402
from jarvis.surfaces.native.marks import MIN_CONFIDENCE, image_size, recognize_text, to_points  # noqa: E402
from jarvis.surfaces.native.surface import ACTIVATED, MENU_OPENED  # noqa: E402

#: Everything this run saw, and how long each part of it took.
RUN = nv.Recorder()


def step(label: str, ok: bool, detail: str = "", *, inconclusive: bool = False) -> bool:
    """Print one result. *inconclusive* (⚠) is for a check that couldn't tell:
    it is not a pass, and the run isn't "all good" with one in it."""
    mark = "⚠" if inconclusive else ("✓" if ok else "✗")
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    RUN.record(label, "inconclusive" if inconclusive else ("pass" if ok else "fail"), detail)
    return ok and not inconclusive


async def timed(name: str, check, surface: NativeSurface, *args):
    """Run *check* (given a surface that times its calls) as one measured run of *name*."""
    with RUN.measure(name, surface) as timed_surface:
        return await check(timed_surface, *args)


async def capture_window(pid: int, number: int) -> Path:
    path = Path(tempfile.mkdtemp()) / "window.png"
    result = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", f"-l{number}", str(path)],
                            capture_output=True, text=True)
    if result.returncode != 0 or not path.exists():
        raise NativeError("screencapture failed — is Screen Recording allowed?")
    return path


def describe_failure(exc: BaseException) -> str:
    """What went wrong, and where: the exception's own words, then the innermost
    line of this repository it passed through. An exception that crosses into
    Objective-C otherwise arrives as a bare message with nothing to say which call raised it."""
    if isinstance(exc, NativeError):
        return exc.message
    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    mine = [f for f in frames if str(f.filename).startswith(str(ROOT))] or frames
    where = f" — at {Path(mine[-1].filename).name}:{mine[-1].lineno} in {mine[-1].name}" if mine else ""
    return f"{type(exc).__name__}: {exc}{where}"


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", text.lower()))


def matching_words(ocr_texts: list[str], window_texts: list[str]) -> list[str]:
    """The words (4+ letters) that both the screenshot's text and the window's own
    accessibility text contain — evidence the OCR read what is really there."""
    return sorted(words(" ".join(ocr_texts)) & words(" ".join(window_texts)))


def window_texts(snap) -> list[str]:
    return [snap.title, *snap.menus, *snap.texts, *(c.label for c in snap.controls),
            *(c.value for c in snap.controls)]


def flat_colour(path: Path) -> bool | None:
    """Whether the picture is a single flat colour (what a window captured without
    Screen Recording permission tends to be); None if that can't be told."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            return all(low == high for low, high in image.convert("RGB").getextrema())
    except Exception:
        return None


async def diagnose_screenshot(surface: NativeSurface, snap, shots: list[Path]) -> list[str]:
    """After "text on a screenshot" fails: find which stage did — the window id,
    the screenshot itself, the OCR — each tried on its own, with what it said."""
    lines = []
    backend = surface.backend
    number = None
    try:
        number = await asyncio.to_thread(backend.window_number, snap.pid, snap.title)
        lines.append(f"window id: {number}" if number else "window id: none found for this window on screen")
    except Exception as exc:
        lines.append("window id: " + describe_failure(exc))
    path = shots[-1] if shots else None
    if path is None and number:
        try:
            path = await capture_window(snap.pid, number)
        except Exception as exc:
            lines.append("screenshot: " + describe_failure(exc))
    if path is not None:
        flat = flat_colour(path)
        try:
            size = image_size(path)
            lines.append(f"screenshot: {size[0]}×{size[1]} px, {path.stat().st_size} bytes"
                         + (" — one flat colour: Screen Recording is probably not allowed for this terminal "
                            "(System Settings → Privacy & Security → Screen Recording)" if flat else ""))
        except Exception as exc:
            lines.append("screenshot: " + describe_failure(exc))
        try:
            boxes = await asyncio.to_thread(recognize_text, path)
            lines.append(f"OCR on that picture: {len(boxes)} pieces of text")
        except Exception as exc:
            import traceback

            tail = " | ".join(line.strip() for line in traceback.format_exc().splitlines()[-4:])
            lines.append(f"OCR on that picture failed: {describe_failure(exc)}  [{tail}]")
    return lines


def ancestors(pid: int, run=subprocess.run) -> list[int]:
    """The processes *pid* was started from, nearest first (a shell, then the terminal app, …)."""
    chain: list[int] = []
    while pid > 1 and len(chain) < 16:
        try:
            parent = int(run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip())
        except (ValueError, OSError, subprocess.SubprocessError):
            break
        if parent <= 1 or parent in chain:
            break
        chain.append(parent)
        pid = parent
    return chain


def host_app(backend, parents: list[int] | None = None) -> str:
    """The app this script is running inside — Terminal, iTerm, an editor: the nearest ancestor
    process that is a regular application. "" when it can't be told (then the frontmost app is looked
    at, as before). This is what the opening ``look`` reads when no ``--app`` is given: a fixed,
    always-present window, not whichever app an earlier check left in front (TextEdit with its
    windows closed, after ``--act``)."""
    named = getattr(backend, "app_for_pid", None)
    if named is None:
        return ""
    for pid in (parents if parents is not None else ancestors(os.getpid())):
        found = named(pid)
        if found:
            return found[1]
    return ""


async def look(surface: NativeSurface, app: str) -> bool:
    print(f"Looking at {app or 'the frontmost app'}")
    try:
        snap, listing = await surface.read(app)
    except NativeError as exc:
        return step("read the window", False, exc.message)
    step("read the window", True, f"{snap.total} controls, {snap.visited} elements visited")
    print("\n" + "\n".join("    " + line for line in listing.splitlines()[:40]) + "\n")
    ok = step("menus", bool(snap.menus), ", ".join(snap.menus[:8]))

    shots: list[Path] = []
    boxes: list = []

    async def capture(pid: int, number: int) -> Path:
        shots.append(await capture_window(pid, number))
        return shots[-1]

    def recognize(path: str):
        boxes.extend(recognize_text(path))
        return boxes

    try:
        marks, marked, overlay = await surface.mark(capture, app, recognize=recognize,
                                                    overlay_dir=Path(tempfile.mkdtemp()))
    except Exception as exc:  # report, don't crash
        ok &= step("text on a screenshot", False, describe_failure(exc))
        for line in await diagnose_screenshot(surface, snap, shots):
            print("      diagnosis: " + line)
        return ok
    # Whether OCR read text is a question about the picture, not about the window's layout:
    # text that sits inside a control (a terminal's one big text area) is named by that
    # control in the marks, so it can't be counted from the marks alone.
    read = [b for b in boxes if b.confidence >= MIN_CONFIDENCE and b.text.strip()]
    outside = [m for m in marks if m.source == "ocr"]
    ok &= step("text on a screenshot", bool(read),
               f"{len(read)} pieces of text read ({len(outside)} outside any control), {len(marks)} marks"
               + (f", overlay at {overlay}" if overlay else ""))
    if not read:
        for line in await diagnose_screenshot(surface, snap, shots):
            print("      diagnosis: " + line)
        return ok
    # And whether it read the right text: the words in the picture should be words the
    # window's own accessibility text has (its title, menus, labels, values).
    common = matching_words([b.text for b in read], window_texts(snap))
    ok &= step("screenshot text matches the window's own text", bool(common),
               f"both contain: {', '.join(common[:8])}" if common else
               f"no word in common. Screenshot: {' · '.join(b.text for b in read[:8])}. "
               f"Window: {' · '.join(t for t in window_texts(snap) if t)[:200]}",
               inconclusive=not common)
    return ok


async def act(surface: NativeSurface) -> bool:
    print("TextEdit round trip")
    launch("TextEdit")
    await pause(2.0)
    try:
        await surface.choose_menu(["File", "New"], "TextEdit")
        await pause(1.0)
        snap, _ = await surface.read("TextEdit")
        area = next((c for c in snap.controls if c.ax_role == "AXTextArea"), None)
        if not step("found the document's text area", area is not None):
            return False
        _, value = await surface.type_into(area.handle, "Hello from JARVIS — café, £5, 😀")
        ok = step("typed Unicode text", "café" in value and "😀" in value, repr(value[:60]))
        await surface.press_key(resolve_key("cmd+a"))
        summary = await surface.choose_menu(["Format", "Font", "Bold"], "TextEdit")
        ok &= step("chose Format › Font › Bold", True, summary)
        await surface.choose_menu(["File", "Close"], "TextEdit")
        ok &= await save_sheet(surface, typed="Hello from JARVIS")
        return ok
    except NativeError as exc:
        return step("TextEdit round trip", False, exc.message)


# --- the sheet TextEdit shows when an edited, unsaved document is closed ---------------------------
#
# What it says depends on the macOS release (older: "Don't Save", "Cancel", "Save"; newer: "Delete",
# "Cancel", "Save…"), so the check does not name a label it expects to find. It asks what the
# surface should always do: see the sheet as a sheet, list its controls ahead of the window's, and
# offer the three ways out — save it, back out, or throw it away. When any of that fails it prints
# what the Accessibility tree really held, so the cause can be read off rather than guessed.

#: What AppKit calls the buttons of a save sheet, in AXIdentifier — a stronger statement of what a
#: button does than its title, which is wording ("Don't Save" once, "Delete" now) and gets localised.
SHEET_BUTTON_IDS = {"discard": ("DontSaveButton",), "cancel": ("CancelButton",), "save": ("OKButton",)}
#: Only if there is no identifier to go by: the titles seen on different macOS releases.
SHEET_BUTTON_LABELS = {"discard": ("don't save", "dont save", "delete", "discard"), "cancel": ("cancel",),
                       "save": ("save",)}


def plain(label: str) -> str:
    return label.lower().replace("’", "'").replace("…", "").replace("...", "").strip()


def sheet_button(controls, kind: str):
    """The sheet's *kind* ("discard" / "cancel" / "save") button and how it was recognised: by
    its AX identifier if any control carries one, else by its title (the first word for save,
    since "Save…" and "Save" are the same button)."""
    for control in controls:
        if control.identifier in SHEET_BUTTON_IDS[kind]:
            return control, f"id {control.identifier}"
    for control in controls:
        title = plain(control.label)
        if title in SHEET_BUTTON_LABELS[kind] or (kind == "save" and title.startswith("save")):
            return control, f"title “{control.label}”"
    return None, ""


def ax_line(backend, element, depth: int) -> str:
    attrs = backend.attributes(element, ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue",
                                          "AXIdentifier", "AXModal", "AXPosition", "AXSize"))
    parts = [str(attrs.get("AXRole") or "?")]
    if attrs.get("AXSubrole"):
        parts[0] += "/" + str(attrs["AXSubrole"])
    for key, mark in (("AXTitle", "title"), ("AXDescription", "desc"), ("AXValue", "value"), ("AXIdentifier", "id")):
        value = attrs.get(key)
        if value not in (None, "") and not isinstance(value, bool):
            parts.append(f'{mark}={str(value)[:40]!r}')
    if attrs.get("AXModal"):
        parts.append("modal")
    position, size = attrs.get("AXPosition"), attrs.get("AXSize")
    if position and size:
        parts.append("frame=({:.0f},{:.0f} {:.0f}x{:.0f})".format(*position, *size))
    else:
        parts.append("no-frame")
    return "  " * depth + " ".join(parts)


def ax_structure(backend, pid: int, *, depth: int = 4, limit: int = 60) -> list[str]:
    """The app's windows, which of them it calls focused (and what that hangs from), and the
    focused window's tree, as Accessibility reports them — what a failing check is judged
    against, not what the surface made of it."""
    application = backend.application(pid)
    windows = backend.windows(application)
    lines = [f"{len(windows)} AX window(s): " + " | ".join(ax_line(backend, w, 0).strip() for w in windows)]
    front = backend.front_window(application)
    if front is None:
        return lines + ["no front window"]
    owner = backend.attribute(front, "AXParent")
    lines.append("AXFocusedWindow: " + ax_line(backend, front, 0).strip()
                 + ("  (one of AXWindows)" if any(backend.same(front, w) for w in windows)
                    else "  (NOT one of AXWindows)")
                 + ("; hangs from: " + ax_line(backend, owner, 0).strip() if owner is not None else ""))
    lines.append("focused window tree:")
    stack = [(front, 0)]
    while stack and len(lines) < limit + 3:
        element, level = stack.pop()
        lines.append(ax_line(backend, element, level + 1))
        if level < depth:
            stack.extend((child, level + 1) for child in reversed(list(backend.attribute(element, "AXChildren") or [])))
    return lines


async def show_structure(surface: NativeSurface, app: str) -> None:
    try:
        found = surface.backend.find_app(app)
        if found is None:
            print(f"      evidence: {app} is not running")
            return
        for line in await asyncio.to_thread(ax_structure, surface.backend, found[0]):
            print("      evidence: " + line)
    except Exception as exc:
        print("      evidence: couldn't dump the tree — " + describe_failure(exc))


#: How long to wait for a sheet to slide down after the menu item that raises it.
SHEET_WAIT_S = 3.0


async def wait_for_sheet(surface: NativeSurface, app: str, timeout_s: float | None = None):
    """Read the window until a sheet shows or *timeout_s* passes; the last reading
    (raises NativeError if the app has no window at all)."""
    deadline = time.monotonic() + (SHEET_WAIT_S if timeout_s is None else timeout_s)
    while True:
        snap, _ = await surface.read(app)
        if any(b.startswith("sheet") for b in snap.blockers) or time.monotonic() >= deadline:
            return snap
        await pause(0.3)


async def save_sheet(surface: NativeSurface, *, typed: str) -> bool:
    """After File ▸ Close on a document with *typed* in it: the save sheet is up and
    listed ahead of the window, offers save / cancel / discard; discarding it closes the document."""
    label = "the save sheet appeared, listed first"
    try:
        snap = await wait_for_sheet(surface, "TextEdit")
    except NativeError as exc:
        step(label, False, f"TextEdit has no window to read after File ▸ Close, so it closed the document "
                           f"without asking: {exc.message}")
        return False
    blocked = [c for c in snap.controls if c.in_blocker]
    flags = [c.in_blocker for c in snap.controls]
    listed_first = all(a >= b for a, b in zip(flags, flags[1:]))
    sheets = [b for b in snap.blockers if b.startswith("sheet")]
    found = {kind: sheet_button(blocked, kind) for kind in ("discard", "cancel", "save")}
    cancel, discard = found["cancel"][0], found["discard"][0]
    missing = [kind for kind, (control, _) in found.items() if control is None]
    seen = (f"blockers: {snap.blockers or 'none'}; the sheet's controls, in listed order: "
            + (", ".join(f'{c.role} “{c.label}”' + (f" [{c.identifier}]" if c.identifier else "") for c in blocked)
               or "none")
            + ("; recognised by " + ", ".join(f"{kind} = “{control.label}” ({how})"
                                               for kind, (control, how) in found.items() if control is not None)
               if blocked else ""))
    problems = []
    if not sheets:
        problems.append("no sheet is open" + ("" if not snap.blockers else " (only a dialog or popover)"))
    elif not blocked:
        problems.append("the sheet lists no controls")
    if blocked and not listed_first:
        problems.append("the sheet's controls are not listed ahead of the window's")
    if sheets and blocked and missing:
        problems.append("the sheet has no " + " / ".join(missing) + " button (by identifier "
                        + "/".join(i for k in missing for i in SHEET_BUTTON_IDS[k]) + ", or by title "
                        + "/".join(f"“{t}”" for k in missing for t in SHEET_BUTTON_LABELS[k]) + ")")
    ok = step(label, not problems, "; ".join([*problems, seen]) if problems else seen)
    if problems:
        await show_structure(surface, "TextEdit")
        if cancel is not None:                       # leave TextEdit as found: back out of the sheet
            await surface.press(cancel.handle)
            print("      the document is still open in TextEdit with the test text; close it and choose Delete")
        else:
            print("      the document may still be open in TextEdit with the test text; close it without saving")
        return False
    await surface.press(discard.handle)
    await pause(0.8)
    try:
        after, _ = await surface.read("TextEdit")
        still = [c for c in after.controls if typed in c.value or typed in c.label]
        leftover = [b for b in after.blockers if b.startswith("sheet")]
        gone = not still and not leftover
        detail = ("no window holds the text and no sheet is left" if gone else
                  f"still open: {', '.join(c.label or c.value[:30] for c in still) or 'a sheet'}"
                  + (f"; sheet {leftover}" if leftover else ""))
    except NativeError as exc:
        gone, detail = True, "TextEdit has no window left (" + exc.message + ")"
    ok &= step("discarding closed the document without saving", gone, detail)
    if not gone:
        await show_structure(surface, "TextEdit")
    return ok


# --- --controls: the rest of what JARVIS does to a window ---------------------------------
#
# Each check names what it's about to try, drives the real surface, and — when a
# step fails — prints the labels the window actually showed, so the output can be
# pasted straight back and the surface fixed from it. They only touch what they
# make themselves: Calculator, an untouched TextEdit document that's cancelled and
# closed, and a throwaway folder on the Desktop that's removed afterwards.

#: What Calculator calls its buttons to accessibility, newest wording first.
CALC_CLEAR = ("all clear", "clear", "ac", "c")
CALC_SEVEN, CALC_ADD, CALC_FIVE, CALC_EQUALS = ("7",), ("add", "+", "plus"), ("5",), ("equals", "=")


def launch(app: str) -> None:
    subprocess.run(["open", "-a", app], check=False)


def open_path(path: Path) -> None:
    subprocess.run(["open", str(path)], check=False)


async def pause(seconds: float) -> None:
    """Let an app catch up. Counted as the check's own waiting, not as JARVIS's time."""
    started = time.monotonic()
    await asyncio.sleep(seconds)
    RUN.add("settle", time.monotonic() - started)


def is_running(app: str) -> bool:
    return subprocess.run(["pgrep", "-x", app], capture_output=True).returncode == 0


def quit_app(app: str) -> None:
    subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'], check=False)


def read_clipboard() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def write_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=False)


def names(controls, limit: int = 40) -> str:
    """What the window showed, for a failing step to print."""
    shown = [f'{c.role} "{c.label}"' if c.label else c.role for c in controls[:limit]]
    return ", ".join(shown) + (f", … ({len(controls)} in all)" if len(controls) > limit else "")


def pick(controls, *wanted: str):
    """The control called exactly one of *wanted* (case aside), earliest wish first."""
    for name in wanted:
        for control in controls:
            if control.label.strip().lower() == name:
                return control
    return None


def shows(snap, text: str) -> bool:
    """Whether *text* is what some piece of the window shows, by itself: the value of a piece of
    static text (what it says, not what the app calls it), or a control's value or name."""
    def clean(value: str) -> str:
        return value.strip().strip("\u200e\u200f\u202a\u202c").strip()
    return (any(clean(i.value) == text or clean(i.text) == text for i in snap.text_items)
            or any(clean(t) == text for t in snap.texts)
            or any(text in (clean(c.value), clean(c.label)) for c in snap.controls))


#: What a text-like element can say, in the order they are printed when a check has to show its working.
TEXT_ATTRIBUTES = ("AXTitle", "AXDescription", "AXValue", "AXValueDescription", "AXRoleDescription",
                   "AXIdentifier")
TEXT_ROLES = {"AXStaticText", "AXTextField", "AXTextArea", "AXHeading", "AXComboBox"}


def text_line(backend, element, depth: int) -> str:
    """One element with every text attribute it has and the Python type each came back as
    (a number and a string are told apart here — ``12`` is not ``'12'``)."""
    attrs = backend.attributes(element, ("AXRole", "AXSubrole", "AXPosition", "AXSize", *TEXT_ATTRIBUTES))
    parts = [str(attrs.get("AXRole") or "?")]
    for key in TEXT_ATTRIBUTES:
        value = attrs.get(key)
        if value not in (None, ""):
            shown = repr(value[:40]) if isinstance(value, str) else repr(value)
            parts.append(f"{key.removeprefix('AX')}={shown}<{type(value).__name__}>")
    position, size = attrs.get("AXPosition"), attrs.get("AXSize")
    parts.append("frame=({:.0f},{:.0f} {:.0f}x{:.0f})".format(*position, *size) if position and size else "no-frame")
    return "  " * depth + " ".join(parts)


def text_structure(backend, pid: int, *, limit: int = 40, depth: int = 12) -> list[str]:
    """Every text-like element in the front window and its text attributes, raw — what a
    check about *reading* text is judged against, not what the surface made of it."""
    window = backend.front_window(backend.application(pid))
    if window is None:
        return ["no front window"]
    lines, stack = [], [(window, 0)]
    while stack and len(lines) < limit:
        element, level = stack.pop()
        role = backend.attribute(element, "AXRole")
        if role in TEXT_ROLES:
            lines.append(text_line(backend, element, min(level, 6)))
        if level < depth:
            stack.extend((child, level + 1) for child in reversed(list(backend.attribute(element, "AXChildren") or [])))
    return lines or ["no text-like element found in the front window"]


#: Attributes that lead *out of* the part of the tree being searched (up, or to the focus), not into it.
LINKS_NOT_FOLLOWED = {"AXParent", "AXTopLevelUIElement", "AXWindow", "AXFocusedUIElement", "AXFocusedWindow",
                      "AXMainWindow", "AXFrontmostWindow"}
DEFAULT_ATTRIBUTES = ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXValueDescription",
                      "AXHelp", "AXIdentifier", "AXPlaceholderValue", "AXSelectedText", "AXChildren",
                      "AXContents", "AXVisibleChildren", "AXPosition", "AXSize")


def plain_text(value) -> str:
    """A value as a person would read it: direction marks and spacing aside, a whole number as itself."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).translate({ord(c): None for c in "\u200e\u200f\u202a\u202c\u2066\u2067\u2068\u2069"}).split())


def locate_text(backend, pid: int, needle: str, snap, *, budget: int = 500) -> list[str]:
    """Where in *pid*'s front window does the app keep *needle*? Every attribute of every element
    reachable through any attribute that leads to other elements (children, contents, rows, …) is
    searched, and each place it turns up is described with whether ``read_window`` listed it and,
    if not, which rule left it out. The answer to "is it an attribute we ignore, an element the
    traversal skips, or something the app never exposes?" — read off the real tree."""
    from jarvis.surfaces.native import ax

    window = backend.front_window(backend.application(pid))
    if window is None:
        return ["no front window"]
    names_of = getattr(backend, "attribute_names", None)
    linked = getattr(backend, "linked_elements", lambda value: None)
    queue = [(window, [], "the window", False)]
    seen: list = []
    found, visited, attributes = [], 0, 0
    while queue and visited < budget:
        element, path, via, under_skipped = queue.pop(0)
        if any(backend.same(element, other) for other in seen):
            continue
        seen.append(element)
        visited += 1
        names = (names_of(element) if names_of else []) or list(DEFAULT_ATTRIBUTES)
        values = backend.attributes(element, tuple(names))
        attributes += len(values)
        role = str(values.get("AXRole") or "?")
        here = [*path, role]
        position, size = values.get("AXPosition"), values.get("AXSize")
        frame = ax.frame_of({"AXPosition": position, "AXSize": size})
        skipped = under_skipped or role in ax.SKIP
        for name, value in values.items():
            if value is None:                      # nothing there - and nothing to hand to the C API
                continue
            links = linked(value)
            if links is not None:
                if name not in LINKS_NOT_FOLLOWED:
                    queue.extend((v, here, name, skipped) for v in links)
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and needle in plain_text(value):
                reasons = []
                if role not in {"AXStaticText", "AXHeading"} and role not in ax.ACTIONABLE:
                    reasons.append(f"{role} is a role the listing does not represent")
                elif role in {"AXStaticText", "AXHeading"} and name != "AXValue":
                    reasons.append(f"it is in {name}, and the listing reads a static text's AXValue")
                if via != "AXChildren" and via != "the window":
                    reasons.append(f"it is reached only through {via}, which the traversal does not follow")
                if frame is not None and frame.empty:
                    reasons.append("its frame has no area, so it is skipped as hidden")
                if skipped:
                    reasons.append("it is inside a container the listing skips")
                described = f"{name}<{type(value).__name__}>={plain_text(value)[:40]!r}"
                found.append((described, here, via, frame, reasons, values))
    shown = shows(snap, needle)
    lines = [f"searched {visited} elements ({attributes} attributes) for “{needle}”; read_window "
             f"{'lists it' if shown else 'does not list it'}"]
    if not found:
        return lines + [f"“{needle}” is in no attribute of anything reachable from the window "
                        "— the app is not exposing it to Accessibility (only the screenshot has it)"
                        + ("" if visited < budget else f"; the search stopped at {budget} elements")]
    for described, path, via, frame, reasons, values in found[:8]:
        label = " ".join(f"{key[2:].lower()}={plain_text(values.get(key))[:30]!r}"
                         for key in ("AXTitle", "AXDescription") if values.get(key))
        geometry = (f"frame=({frame.x:.0f},{frame.y:.0f} {frame.w:.0f}x{frame.h:.0f})"
                    if frame is not None else "no-frame")
        verdict = "listed" if shown and not reasons else ("not listed: " + "; ".join(reasons or ["no rule explains it"]))
        lines.append(f"{described} on {path[-1]}{(' ' + label) if label else ''} — {' › '.join(path)}, "
                     f"via {via}, {geometry} — {verdict}")
    return lines


async def show_text_location(surface: NativeSurface, app: str, needle: str, snap) -> None:
    try:
        found = surface.backend.find_app(app)
        if found is None:
            print(f"      evidence: {app} is not running")
            return
        for line in await asyncio.to_thread(locate_text, surface.backend, found[0], needle, snap):
            print("      evidence: " + line)
    except Exception as exc:
        print("      evidence: couldn't search the tree — " + describe_failure(exc))


async def show_text_structure(surface: NativeSurface, app: str) -> None:
    try:
        found = surface.backend.find_app(app)
        if found is None:
            print(f"      evidence: {app} is not running")
            return
        for line in await asyncio.to_thread(text_structure, surface.backend, found[0]):
            print("      evidence: " + line)
    except Exception as exc:
        print("      evidence: couldn't dump the text — " + describe_failure(exc))


async def guarded(label: str, check) -> bool:
    try:
        return await check
    except NativeError as exc:
        return step(label, False, exc.message)
    except Exception as exc:  # report, don't crash: the rest of the checks still run
        return step(label, False, f"{type(exc).__name__}: {exc}")


async def press_button(surface: NativeSurface, app: str, *labels: str, required: bool = True) -> bool:
    snap, _ = await surface.read(app)
    control = pick(snap.controls, *labels)
    if control is None:
        if required:
            return step(f"found the {labels[0]} button", False, "controls: " + names(snap.controls))
        return True
    await surface.press(control.handle)
    return True


async def copied_display(surface: NativeSurface) -> str:
    """Calculator's display, copied with ⌘C — a reading that doesn't depend on
    how Accessibility happens to name the display."""
    await surface.press_key(resolve_key("cmd+c"))
    await pause(0.3)
    with RUN.verifying():
        return read_clipboard().strip()


async def calculator_click(surface: NativeSurface) -> bool:
    print("click_control — 7 + 5 = in Calculator")
    if not await press_button(surface, "Calculator", *CALC_CLEAR, required=False):
        return False
    for labels in (CALC_SEVEN, CALC_ADD, CALC_FIVE, CALC_EQUALS):
        if not await press_button(surface, "Calculator", *labels):
            return False
    result = await copied_display(surface)
    ok = step("pressing the buttons worked the sum", result == "12", f"the display copied as {result!r}")
    snap, _ = await surface.read("Calculator")
    shown = shows(snap, "12")
    seen = ("text items (name → shown): "
            + ", ".join(f"{i.label or '—'} → {i.value or '—'}" for i in snap.text_items[:8])
            + f"; controls: {names(snap.controls, 14)}")
    ok &= step("read_window shows the result", shown, seen)
    if not shown:
        await show_text_location(surface, "Calculator", "12", snap)
        await show_text_structure(surface, "Calculator")
    return ok


def fit_axis(pairs: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares ``point = a + slope * pixel`` through (pixel, point) pairs: ``(a, slope, worst
    miss)``; None when the pixels don't vary (one column of text says nothing about the scale across)."""
    if len(pairs) < 2:
        return None
    mean_u, mean_v = sum(u for u, _ in pairs) / len(pairs), sum(v for _, v in pairs) / len(pairs)
    spread = sum((u - mean_u) ** 2 for u, _ in pairs)
    if spread < 1.0:
        return None
    slope = sum((u - mean_u) * (v - mean_v) for u, v in pairs) / spread
    a = mean_v - slope * mean_u
    return a, slope, max(abs(a + slope * u - v) for u, v in pairs)


def coordinate_report(capture, pairs: list[tuple[tuple[float, float], tuple[float, float]]],
                      windows: list | None = None, screens: list | None = None) -> list[str]:
    """What relates the screenshot's pixels to the controls' points, measured: the rectangles the two
    window descriptions give, the scale each implies, and the scale and origin the text and the
    controls it names actually fit. *pairs* are ((pixel x, pixel y), (point x, point y)) for the
    centre of each piece of text and the centre of the control called the same."""
    from jarvis.surfaces.native.marks import pixels_per_point

    def rect(frame) -> str:
        return "none" if frame is None else f"({frame.x:.0f}, {frame.y:.0f}, {frame.w:.0f}×{frame.h:.0f})"

    width, height = capture.size
    lines = [f"Accessibility's window {rect(capture.ax)}; the window server's {rect(capture.bounds)}; "
             f"the picture {width}×{height} px, converted against the {capture.source}'s "
             + "{:.2f}×{:.2f} px/pt".format(*pixels_per_point(capture.frame, capture.size))]
    if capture.disagreement:
        lines.append("the two disagree: " + capture.disagreement)
    across = fit_axis([(p[0][0], p[1][0]) for p in pairs])
    down = fit_axis([(p[0][1], p[1][1]) for p in pairs])
    if across is None or down is None:
        lines.append(f"{len(pairs)} text/control pair(s): too few, or all in one row or column, to fit a scale")
    else:
        (left, slope_x, miss_x), (top, slope_y, miss_y) = across, down
        covers_w, covers_h = width * slope_x, height * slope_y
        lines.append(f"fitted to {len(pairs)} pairs: {1 / slope_x:.2f} px/pt across, {1 / slope_y:.2f} down; "
                     f"the controls put the picture at ({left:.0f}, {top:.0f}, {covers_w:.0f}×{covers_h:.0f}) "
                     f"(worst miss {max(miss_x, miss_y):.1f} pt)")
        if abs(slope_x / slope_y - 1.0) > 0.02:
            lines.append("the scale across and down differ, so no one window rectangle explains the controls: "
                         "the controls and the picture are not the same layout (or the text read is not what it names)")
        else:
            fitted = (left, top, covers_w, covers_h)
            for name, frame in (("the window server's", capture.bounds), ("Accessibility's", capture.ax)):
                if frame is not None and max(abs(a - b) for a, b in zip(fitted, (frame.x, frame.y, frame.w, frame.h))) <= 3:
                    lines.append(f"the controls fit {name} rectangle")
                    break
            else:
                lines.append("the controls fit neither window rectangle: the picture is of something "
                             "other than the window those rectangles describe")
    for window in (windows or [])[:6]:
        x, y, w, h = window["bounds"]
        lines.append(f"window {window['number']} “{window['name']}” layer {window['layer']} "
                     f"{'on screen' if window['on_screen'] else 'off screen'} ({x:.0f}, {y:.0f}, {w:.0f}×{h:.0f})")
    if screens:
        lines.append("displays: " + "; ".join(
            "({:.0f}, {:.0f}, {:.0f}×{:.0f}) at {:g}×".format(*screen["frame"], screen["scale"]) for screen in screens))
    return lines


async def calculator_marks(surface: NativeSurface) -> bool:
    print("mark_screen / click_mark — Calculator")
    seen: list[tuple[Path, list]] = []

    def recognize(path: str):
        boxes = recognize_text(path)
        seen.append((Path(path), boxes))
        return boxes

    marks, _, overlay = await surface.mark(capture_window, "Calculator", recognize=recognize,
                                           overlay_dir=Path(tempfile.mkdtemp()))
    from_text = sum(1 for m in marks if m.source == "ocr")
    ok = step("the window was numbered", bool(marks),
              f"{len(marks)} marks, {from_text} of them found by reading text")
    ok &= step("the marked picture exists for find_on_screen to choose from",
               overlay is not None and overlay.exists(),
               str(overlay) if overlay else "none made — mark overlays need Pillow (pip install pillow)")

    # A screenshot is in pixels and a click is in points; the ratio between them
    # is what has to be right for a click on text to land on the text. Compare:
    # each piece of text that's also a control's name should sit inside that control.
    # (Against the rectangle mark_screen itself converted with — surface.last_capture.)
    snap, _ = await surface.read("Calculator")
    path, boxes = seen[-1]
    size = image_size(path)
    covered = surface.last_capture
    window = covered.frame if covered is not None else snap.frame
    compared, off, pairs = 0, [], []
    for box in boxes:
        said = box.text.strip().lower()
        control = next((c for c in snap.controls if c.frame is not None
                        and c.label.strip().lower() == said), None)
        if box.confidence < MIN_CONFIDENCE or control is None or window is None:
            continue
        compared += 1
        x, y = to_points(box, size, window).center
        f = control.frame
        pairs.append(((box.x + box.w / 2, box.y + box.h / 2), f.center))
        if not (f.x - 2 <= x <= f.x + f.w + 2 and f.y - 2 <= y <= f.y + f.h + 2):
            off.append(f'“{box.text}” read at ({x:.0f}, {y:.0f}), the control is at '
                       f'({f.x:.0f}, {f.y:.0f}, {f.w:.0f}×{f.h:.0f})')
    agreed = step("text read off the screenshot lands on the controls it names",
                  compared > 0 and not off,
                  f"{compared - len(off)} of {compared} agree" + (f"; {'; '.join(off[:3])}" if off else "")
                  if compared else f"no text matched a control's name ({len(boxes)} pieces of text read)")
    ok &= agreed
    if not agreed and covered is not None:
        backend = surface.backend
        windows = await asyncio.to_thread(getattr(backend, "window_list", lambda pid: []), snap.pid)
        screens = await asyncio.to_thread(getattr(backend, "screens", lambda: []))
        for line in coordinate_report(covered, pairs, windows, screens):
            print("      evidence: " + line)

    nine = next((m for m in marks if m.label.strip() == "9"), None)
    if not step("found the 9 mark", nine is not None, "marks: " + ", ".join(m.line() for m in marks[:20])):
        return False
    await surface.click_mark(nine.number)
    await pause(0.4)
    result = await copied_display(surface)
    return step("clicking the mark pressed the button", result == "9", f"the display copied as {result!r}") and ok


async def calculator(surface: NativeSurface) -> bool:
    was_running, saved = is_running("Calculator"), read_clipboard()
    launch("Calculator")
    await pause(1.5)
    try:
        ok = await guarded("click_control in Calculator", timed("calculator click", calculator_click, surface))
        ok &= await guarded("mark_screen in Calculator", timed("calculator marks", calculator_marks, surface))
        return ok
    finally:
        write_clipboard(saved)
        if not was_running:
            quit_app("Calculator")


async def close_untitled_textedit(surface: NativeSurface) -> None:
    """Back out of the document the check made, without saving anything: cancel
    the Save sheet if it's up, then close the (untouched, so never asked about)
    window. Once only — a second Close could be someone's own document."""
    try:
        snap, _ = await surface.read("TextEdit")
        cancel = pick(snap.controls, "cancel")
        if cancel is not None:
            await surface.press(cancel.handle)
            await pause(0.5)
        await surface.choose_menu(["File", "Close"], "TextEdit")
        await pause(0.6)
    except NativeError as exc:
        step("closed the document the check made", False, exc.message)


def option_facts(backend, option) -> dict[str, str]:
    """Everything Accessibility says about one menu item that can be said in a word — each scalar
    attribute — plus where it hangs and where it sits in its menu.

    Nothing here is handed to the C API but through ``backend`` (``linked_elements``), and only
    after the missing attributes are set aside: an attribute an element doesn't have comes back as
    None, and None must never reach CoreFoundation (``CFGetTypeID(NULL)`` is a segmentation fault)."""
    element = option.element
    names = (getattr(backend, "attribute_names", lambda e: [])(element)) or list(DEFAULT_ATTRIBUTES)
    facts: dict[str, str] = {}
    for name, value in backend.attributes(element, tuple(names)).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, tuple) and name in {"AXPosition", "AXSize"}:
            facts[name] = ",".join(f"{v:g}" for v in value)
        elif isinstance(value, bool):                          # enabled, selected, focused: a difference that counts
            facts[name] = str(value)
        elif isinstance(value, (str, int, float)):             # a word or a number; links and arrays are not
            facts[name] = plain_text(value)
    parent = backend.attribute(element, "AXParent")
    if parent is not None:
        siblings = list(backend.attribute(parent, "AXChildren") or [])
        facts["parent"] = f"{backend.attribute(parent, 'AXRole')} with {len(siblings)} children"
    facts["place in the menu"] = f"item {option.index}" + (f", under “{option.section}”" if option.section else "")
    return facts


def option_evidence(backend, candidates) -> list[str]:
    """Why two menu items that read the same are two: each one's attributes, and the ones that differ."""
    facts = [option_facts(backend, option) for option in candidates]
    lines = [f"candidate {n}: " + " ".join(f"{k.removeprefix('AX')}={v!r}" for k, v in fact.items())
             for n, fact in enumerate(facts, 1)]
    keys = sorted({k for fact in facts for k in fact})
    apart = [k for k in keys if len({fact.get(k) for fact in facts}) > 1]
    where = {"AXPosition", "AXSize", "AXFrame", "place in the menu"}
    real = [k for k in apart if k not in where]
    lines.append("they differ in: " + (", ".join(apart) if apart else "nothing at all")
                 + ("" if real else " — only where they sit; nothing about what they are or do tells them apart"))
    return lines


async def textedit_dropdown(surface: NativeSurface) -> bool:
    print("choose_option — the Where pop-up on TextEdit's Save sheet")
    was_running, made = is_running("TextEdit"), False
    launch("TextEdit")
    await pause(2.0)
    try:
        await surface.choose_menu(["File", "New"], "TextEdit")
        made = True
        await pause(1.0)
        await surface.choose_menu(["File", "Save"], "TextEdit")
        await pause(1.2)
        snap, _ = await surface.read("TextEdit")
        popups = [c for c in snap.controls if c.ax_role == "AXPopUpButton"]
        if not step("the Save sheet lists a pop-up button", bool(popups),
                    ", ".join(f'“{c.label}”' for c in popups) or "controls: " + names(snap.controls)):
            return False
        popup = next((c for c in popups if "where" in c.label.lower()), popups[0])
        try:
            summary = await surface.choose_option(popup.handle, "Desktop")
        except AmbiguousOption as exc:
            # Where: lists "Desktop — iCloud" under both its iCloud and its Favourites headings. Two
            # items nothing in the request tells apart must be refused, not guessed; the postcondition
            # is unchanged — once one is named, the pop-up has to read Desktop.
            step("an option the menu lists twice is refused, not guessed",
                 len(exc.candidates) >= 2, exc.message)
            for line in await asyncio.to_thread(option_evidence, surface.backend, exc.candidates):
                print("      evidence: " + line)
            summary = await surface.choose_option(popup.handle, "Desktop", occurrence=1)
        await pause(0.8)
        snap, _ = await surface.read("TextEdit")
        again = next((c for c in snap.controls if c.ax_role == "AXPopUpButton"
                      and c.label == popup.label), None)
        value = ""
        if again is not None:
            value = again.value or str((await surface.describe(again.handle)).get("value") or "")
        return step("the pop-up now says Desktop", "desktop" in value.lower(),
                    f"{summary} It reads {value!r}.")
    finally:
        if made:
            await close_untitled_textedit(surface)
        if not was_running:
            quit_app("TextEdit")


def under_pointer(backend, point: tuple[float, float]) -> str:
    """The element Accessibility reports at a screen point — and what it hangs from — which is
    what a press there lands on, as opposed to what the check meant it to land on."""
    finder = getattr(backend, "element_at", None)
    if finder is None:
        return "(this backend can't say)"
    element = finder(*point)
    if element is None:
        return "nothing"
    chain, seen = [], 0
    while element is not None and seen < 4:
        chain.append(ax_line(backend, element, 0).split(" frame=")[0])
        element, seen = backend.attribute(element, "AXParent"), seen + 1
    return " ‹ ".join(chain)


def selection_after(snap) -> str:
    """Which of the two rows Finder shows selected: a press that took hold of the file selects it,
    so "the file's row is selected" says the press landed on the file and the drop was what failed."""
    parts = []
    for name, word in (("drag-me.txt", "the file's row"), ("target", "the folder's row")):
        rows = [c for c in snap.controls if c.ax_role == "AXRow" and c.label.lower().startswith(name)]
        parts.append(f"{word} is " + ("not shown" if not rows else "selected" if rows[0].selected else "not selected"))
    return ", ".join(parts)


def drag_evidence(surface: NativeSurface, base: Path, source: Path, target: Path) -> str:
    """After a drag that moved nothing: where the files are, and where the pointer pressed and
    let go (and what was under it) — enough to tell a drop the app refused from a press that
    never took hold of the file."""
    parts = [f"on disk: {sorted(str(p.relative_to(base)) for p in base.rglob('*'))}",
             f"{source} {'still exists' if source.exists() else 'is gone'}; "
             f"{target / source.name} {'exists' if (target / source.name).exists() else 'does not exist'}"]
    drag = surface.last_drag
    if drag:
        for key, verb, frame_key in (("from", "pressed", "source"), ("to", "released", "target")):
            point, frame = drag[key], drag[frame_key]
            where = f" in the row at ({frame.x:.0f}, {frame.y:.0f}, {frame.w:.0f}×{frame.h:.0f})" if frame else ""
            parts.append(f"{verb} at ({point.x:.0f}, {point.y:.0f}) on its {point.on}{where}; "
                         f"under that point: {under_pointer(surface.backend, point.point)}")
    return "; ".join(parts)


async def finder_drag(surface: NativeSurface) -> bool:
    print("drag_control — a file onto a folder in Finder")
    base = Path.home() / "Desktop" / f"JARVIS-check-{os.getpid()}"
    source, target = base / "drag-me.txt", base / "target"
    target.mkdir(parents=True)
    source.write_text("dragged by JARVIS's check_native.py\n")
    try:
        open_path(base)
        await pause(2.0)
        try:
            await surface.choose_menu(["View", "as List"], "Finder")
            await pause(0.8)
            step("switched the Finder window to a list", True)
        except NativeError as exc:      # the rows are easier to tell apart as a list, not essential
            step("switched the Finder window to a list", False, exc.message)
        snap, _ = await surface.read("Finder")

        def row(name: str):
            hits = [c for c in snap.controls if c.label.lower().startswith(name)]
            return next((c for c in hits if c.ax_role == "AXRow"), hits[0] if hits else None)

        file, folder = row("drag-me.txt"), row("target")
        if not step("found the file and the folder in the window", file is not None and folder is not None,
                    "controls: " + names(snap.controls)):
            return False
        summary = await surface.drag(file.handle, folder.handle)
        # The same postcondition, looked at until it holds or four seconds have passed: Finder (and an
        # iCloud-backed Desktop) can take a moment to finish a move it has accepted.
        moved = False
        for _ in range(16):
            await pause(0.25)
            with RUN.verifying():
                moved = (target / "drag-me.txt").exists() and not source.exists()
            if moved:
                break
        detail = summary
        if not moved:
            detail += " " + await asyncio.to_thread(drag_evidence, surface, base, source, target)
            with contextlib.suppress(NativeError):
                after, _ = await surface.read("Finder")
                detail += "; afterwards " + selection_after(after)
        return step("the file is inside the folder", moved, detail)
    finally:
        with contextlib.suppress(NativeError):
            front, _ = await surface.read("Finder")
            if front.title == base.name:        # only the window this check opened
                await surface.choose_menu(["File", "Close Window"], "Finder")
        shutil.rmtree(base, ignore_errors=True)


async def controls(surface: NativeSurface) -> bool:
    ok = await calculator(surface)
    ok &= await guarded("choose_option in TextEdit", timed("save sheet pop-up", textedit_dropdown, surface))
    ok &= await guarded("drag_control in Finder", timed("finder drag", finder_drag, surface))
    return ok


# --- --stale: re-finding a control that has gone stale, on a real Mac ----------------------------
#
# The surface re-finds a stale handle only when exactly one control of the same
# kind, name and place is left (NativeSurface._relocate). Whether macOS hands back
# a stale reference when an app rebuilds a control — and what the rebuilt one looks
# like — is the open question, so these use a window (scripts/ax_fixture.py) that
# rebuilds on command and keeps its own record of what was pressed. The failure that
# matters is not "couldn't find Save"; it is "found the wrong Save and pressed it".

FIXTURE = "JARVIS Fixture"


class FixtureProcess:
    """scripts/ax_fixture.py in a process of its own, driven through two files."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.log_path, self.commands_path = self.directory / "fixture.log", self.directory / "fixture.cmd"
        self.commands_path.write_text("")
        self.process: subprocess.Popen | None = None
        self.pid = 0
        self._sequence = 0

    def lines(self) -> list[str]:
        try:
            return self.log_path.read_text().splitlines()
        except OSError:
            return []

    def start(self, timeout_s: float = 15.0) -> int:
        errors = (self.directory / "fixture.err").open("w")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "ax_fixture.py"), "--log", str(self.log_path),
             "--commands", str(self.commands_path)], stdout=subprocess.DEVNULL, stderr=errors)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ready = next((line for line in self.lines() if line.startswith("ready:")), None)
            if ready:
                self.pid = int(ready.split(":")[1])
                return self.pid
            if self.process.poll() is not None:
                raise RuntimeError("the fixture exited: " + (self.directory / "fixture.err").read_text()[-300:])
            time.sleep(0.1)
        raise RuntimeError(f"the fixture window didn't appear within {timeout_s:.0f} s")

    def send(self, command: str, timeout_s: float = 5.0) -> dict:
        self._sequence += 1
        with self.commands_path.open("a") as handle:
            handle.write(f"{self._sequence} {command}\n")
        ack, failed = f"ack:{self._sequence}:", f"error:{self._sequence}:"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for line in self.lines():
                if line.startswith(ack):
                    time.sleep(0.2)                   # a moment for Accessibility to see the change
                    return json.loads(line[len(ack):])
                if line.startswith(failed):
                    raise RuntimeError(f"“{command}” failed: {line[len(failed):]}")
            time.sleep(0.02)
        raise RuntimeError(f"the fixture didn't answer “{command}” within {timeout_s:.0f} s")

    def events(self) -> list[str]:
        return [line for line in self.lines() if line.startswith(("click:", "toggle:"))]

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.send("quit", timeout_s=1.0)
        if self.process is not None:
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.terminate()


def parse_event(line: str) -> dict:
    """``click:Save:save:born=3`` → what was pressed, and which build of it."""
    kind, name, identifier, born = line.split(":", 3)
    return {"kind": kind, "name": name, "identifier": identifier, "born": int(born.split("=")[1])}


def describe_events(events: list[dict]) -> str:
    return ", ".join(f"{e['kind']} “{e['name']}” ({e['identifier']}, build {e['born']})" for e in events) or "nothing"


def judge(scenario: str, outcome: dict) -> tuple[str, str]:
    """Whether a stale-handle scenario went as it must: ("ok" | "inconclusive" |
    "fail", why). *outcome* has the error JARVIS raised (or None), the presses the
    window itself recorded, how many handles were re-found or refused during the
    press, and the builds of the control before and after the change."""
    events, error = outcome["events"], outcome["error"]
    builds = {event["born"] for event in events}
    survived = bool(events) and builds == {outcome["original"]}
    if scenario in {"rebuild", "twin"}:
        if error:
            return "fail", f"refused although exactly one Save is the same one: {error}"
        if survived:
            return "inconclusive", "macOS kept the old reference valid, so there was nothing to re-find"
        if (len(events) == 1 and builds == {outcome["changed"]} and outcome["relocated"] == 1
                and events[0]["identifier"] == "save"):
            return "ok", "the stale handle was re-found and the rebuilt button pressed"
        return "fail", f"pressed {describe_events(events)}; re-found {outcome['relocated']} handle(s)"
    if not events:                                  # every other scenario must press nothing
        if error and outcome["refused"] >= 1:
            return "ok", f"refused, and nothing was pressed: {error}"
        if error:
            return "inconclusive", f"nothing pressed, but not by re-finding failing: {error}"
        return "fail", "no error, and no press either"
    if survived:
        return "inconclusive", "macOS kept the old reference valid, so the press went to the original"
    return "fail", (f"PRESSED {describe_events(events)} — a control it could not be sure was the one "
                    "that was meant")


async def fixture_handle(surface: NativeSurface) -> tuple[str | None, str]:
    snap, _ = await surface.read(FIXTURE)
    control = next((c for c in snap.controls if c.label == "Save"), None)
    return (control.handle if control else None), names(snap.controls)


async def stale_scenario(surface: NativeSurface, fixture: FixtureProcess, command: str) -> dict | str:
    """Start from one Save, take a handle to it, make the app change it as
    *command* says, press the handle. Returns what happened, or why it couldn't run."""
    original = (await asyncio.to_thread(fixture.send, "restore"))["generation"]
    handle, seen = await fixture_handle(surface)
    if handle is None:
        return "no Save button to take a handle to; the window showed: " + seen
    with RUN.verifying():
        before = len(fixture.events())
    relocated, refused = surface.relocations, surface.relocations_refused
    changed = (await asyncio.to_thread(fixture.send, command))["generation"]
    error = None
    try:
        await surface.press(handle)
    except NativeError as exc:
        error = exc.message
    await pause(0.5)
    with RUN.verifying():
        events = [parse_event(line) for line in fixture.events()[before:]]
    return {"original": original, "changed": changed, "error": error, "events": events,
            "relocated": surface.relocations - relocated, "refused": surface.relocations_refused - refused}


STALE_SCENARIOS = (
    ("rebuild", "rebuild", "a rebuilt button is re-found and pressed"),
    ("twin", "twin", "two buttons told apart by identifier: the right one is pressed"),
    ("duplicate", "duplicate", "two identical buttons: none is guessed"),
    ("impostor", "impostor", "a look-alike of another kind is not pressed"),
    ("rename", "rename Delete", "a button renamed in place is not pressed"),
    ("move", "move", "the same name in another window is not pressed"),
)


class PressTrace:
    """What one ``surface.press`` actually did, seen from the seams it acts through. Nothing is added to
    the press: the wrappers record and pass through. They record ``backend.actions`` (what the element
    offered), ``backend.perform`` (each Accessibility action, and whether the app accepted it) and
    ``input.click`` (a synthetic mouse click - only ever the fallback, and always after bringing the app
    forward). The app activating *itself* in response to an action is invisible here by design: that is
    exactly what is left over when none of these shows JARVIS doing it."""

    _missing = object()

    def __init__(self, surface: NativeSurface):
        self.surface = surface
        self.offered: list[list[str]] = []
        self.performed: list[tuple[str, bool]] = []
        self.clicks: list[tuple] = []
        self._restore: list[tuple] = []

    def __enter__(self) -> PressTrace:
        backend, device = self.surface.backend, self.surface.input
        actions, perform, click = backend.actions, backend.perform, device.click

        def traced_actions(element):
            found = actions(element)
            self.offered.append(list(found))
            return found

        def traced_perform(element, action):
            accepted = perform(element, action)
            self.performed.append((str(action), bool(accepted)))
            return accepted

        def traced_click(*args, **kwargs):
            self.clicks.append((args, kwargs))
            return click(*args, **kwargs)

        for owner, name, wrapper in ((backend, "actions", traced_actions), (backend, "perform", traced_perform),
                                     (device, "click", traced_click)):
            self._restore.append((owner, name, vars(owner).get(name, self._missing)))
            setattr(owner, name, wrapper)
        return self

    def __exit__(self, *exc: object) -> None:
        for owner, name, original in reversed(self._restore):
            if original is self._missing:
                delattr(owner, name)
            else:
                setattr(owner, name, original)

    @property
    def axpress(self) -> bool | None:
        """Whether the app accepted AXPress: True/False, or None if it was never tried."""
        tried = [accepted for action, accepted in self.performed if action == "AXPress"]
        return any(tried) if tried else None

    @property
    def offered_axpress(self) -> bool:
        return any("AXPress" in actions for actions in self.offered)


def press_verdict(trace: PressTrace, summary: str, error: str, pressed: int, behind: bool,
                  front: str) -> tuple[bool, str]:
    """Pass or fail for a press made while *front* was the app in front, and the mechanism that says why.
    Passes only for a semantic AXPress that the app accepted, that the app recorded, and that left *front*
    in front. Everything else fails, saying which of the ways it went wrong it was."""
    where = f"{front} " + ("stayed in front" if behind else "was no longer in front")
    counted = f"the window recorded {pressed} press(es); {where}"
    said = f"the surface returned {summary!r}" if summary else "the surface returned nothing"
    if trace.axpress is True:
        how = "AXPress succeeded"
    elif trace.axpress is False:
        how = "AXPress was refused by the app (perform returned False)"
    elif trace.offered_axpress:
        how = "AXPress was offered but never tried"
    else:
        how = "the button does not offer AXPress" + ("" if trace.offered else " (no action list was read)")
    if trace.clicks:
        if trace.axpress is True:      # not a path the surface has: say so rather than guess at it
            return False, f"AXPress succeeded and a coordinate click was posted as well — {said}; {counted}"
        sentence = ("the fallback click brought the app forward" if not behind
                    else "the fallback click ran, the app did not come forward")
        return False, (f"AXPress did not do the press: {how}, so the surface fell back to a coordinate click; "
                       f"{sentence} — {said}; {counted}")
    if error:
        tried = f" — {how}" if trace.performed or trace.offered else ""
        return False, f"the press raised instead of acting: {error}{tried}; {counted}"
    others = [action for action, accepted in trace.performed if action != "AXPress" and accepted]
    if trace.axpress is not True:
        return False, (f"not an AXPress: {how}; " + (f"it was done by {', '.join(others)}" if others
                                                     else "and no click was posted either") + f" — {said}; {counted}")
    if pressed == 0:
        return False, f"AXPress succeeded but the app recorded no press — {said}; {counted}"
    if not behind:
        return False, (f"AXPress succeeded, JARVIS posted no click and activated nothing, and the app still came "
                       f"forward: AXPress (or the app's handling of it) activated the target — the fixture's own "
                       f"action only logs a line — {said}; {counted}")
    return True, f"{how}, with no click and no activation — {said}; {counted}"


async def background_press(surface: NativeSurface, fixture: FixtureProcess) -> bool:
    """A press by Accessibility needs no focus: with Finder in front, pressing the fixture's button does
    it without taking the front. Activation is a failure, and so is any other way of getting the press
    done: the verdict names the mechanism (see :func:`press_verdict`). When the press worked and the app
    came forward anyway, the controlled trials of :func:`localise_activation` say whose doing that is;
    they add evidence, they never change the verdict."""
    label = "a press works with another app in front"
    backend = surface.backend
    finder = backend.find_app("Finder")
    if finder is None:
        return step(label, False, "no Finder to put in front")
    await asyncio.to_thread(fixture.send, "restore")
    handle, seen = await fixture_handle(surface)
    if handle is None:
        return step(label, False, "the window showed: " + seen)
    trace, summary, error, came_forward, lines, started = await front_and_press(
        surface, finder[0], lambda: try_press(surface, handle), fixture)
    pressed = sum(1 for line in lines if line.startswith(("click:", "toggle:")))
    ok, detail = press_verdict(trace, summary, error, pressed, not came_forward, "Finder")
    result = step(label, ok, detail)
    if not ok and trace.axpress is True and not trace.clicks and pressed and came_forward:
        primary = Trial("the Save button, pressed by JARVIS", accepted=True, recorded=True, came_forward=True,
                        **activation_facts(lines, started))
        target = backend.find_app(FIXTURE)
        for line in (await localise_activation(surface, fixture, finder[0], target[0] if target else 0, primary)):
            print("      evidence: " + line)
    return result


# --- who activates the app on a press: the trials behind the --stale evidence ---------------------------
#: The same press made by a client with nothing of JARVIS in it: a bare AXUIElementPerformAction in a
#: process of its own, on the fixture's button whose identifier is argv[2].
RAW_PRESS = """
import sys
import ApplicationServices as AS
pid, identifier = int(sys.argv[1]), sys.argv[2]


def get(element, name):
    error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


stack = list(get(AS.AXUIElementCreateApplication(pid), "AXWindows") or [])
while stack:
    element = stack.pop()
    if get(element, "AXIdentifier") == identifier:
        error = AS.AXUIElementPerformAction(element, "AXPress")
        print("AXUIElementPerformAction ->", error)
        sys.exit(0 if error == 0 else 3)
    stack.extend(get(element, "AXChildren") or [])
print("no element with identifier", identifier)
sys.exit(4)
"""


class Trial:
    """One controlled press with Finder in front, and what it did."""

    def __init__(self, name: str, *, accepted: bool | None = None, recorded: bool | None = None,
                 came_forward: bool | None = None, handler: dict[str, str] | None = None,
                 delay_s: float | None = None, note: str = ""):
        self.name = name
        self.accepted = accepted           # the app accepted the press (AXPress returned success)
        self.recorded = recorded           # the app's own log shows its action ran (None: it has none)
        self.came_forward = came_forward   # something other than Finder was in front afterwards
        self.handler = handler             # the fixture's state when its action began
        self.delay_s = delay_s             # press start -> the fixture logging that it became active
        self.note = note                   # why the trial could not be run or is not to be trusted
        self.watching = True               # the fixture could register for its own activation notices

    @property
    def self_reported(self) -> bool | None:
        """Whether the app itself said it became active (its own didBecomeActive, which does not come
        from Accessibility): True/False for the fixture's trials, None where there is no such witness."""
        if self.recorded is None or not self.watching:
            return None
        return self.delay_s is not None

    @property
    def usable(self) -> bool:
        return self.accepted is True and not self.note

    def describe(self) -> str:
        if self.note:
            return f"{self.name}: not measured — {self.note}"
        parts = ["accepted" if self.accepted else "not accepted"]
        if self.recorded is not None:
            parts.append("action ran" if self.recorded else "action did not run")
        if self.handler:
            parts.append("app already active when its action began" if self.handler.get("active") == "1"
                         else "app not yet active when its action began")
        if self.delay_s is not None:
            parts.append(f"became active {self.delay_s * 1000:.0f} ms after the press began")
        elif self.self_reported is False and self.came_forward:
            parts.append("the app itself logged no activation")
        parts.append("the app came forward" if self.came_forward else "the app stayed behind")
        return f"{self.name}: " + ", ".join(parts)


def log_fields(line: str) -> dict[str, str]:
    """``handler:click:active=0:key=1:t=12.5`` -> {"active": "0", "key": "1", "t": "12.5"}."""
    return {key: value for key, _, value in (field.partition("=") for field in line.split(":")[1:]) if value}


def activation_facts(lines: list[str], started: float) -> dict:
    """What the fixture's own log says about a press that began at monotonic time *started*: the state
    its action found, and how soon after the press began it logged becoming active."""
    handlers = [log_fields(line) for line in lines if line.startswith("handler:")]
    became = [float(log_fields(line)["t"]) - started for line in lines
              if line.startswith("activation:became") and "t" in log_fields(line)]
    return {"handler": handlers[-1] if handlers and "active" in handlers[-1] else None,
            "delay_s": min((d for d in became if d >= 0), default=None)}


async def try_press(surface: NativeSurface, handle: str) -> tuple[str, str]:
    try:
        return await surface.press(handle), ""
    except NativeError as exc:
        return "", exc.message


async def front_and_press(surface: NativeSurface, finder_pid: int, press, fixture=None):
    """Put Finder in front, make the press, and see who is in front half a second later. Returns the
    trace of what the surface did, what it returned, any error, whether something other than Finder came
    forward, the fixture's new log lines, and when the press began (``time.monotonic()``)."""
    backend = surface.backend
    await asyncio.to_thread(backend.activate, finder_pid)
    await asyncio.to_thread(wait_front, backend, finder_pid)
    before = len(await asyncio.to_thread(fixture.lines)) if fixture is not None else 0
    started = time.monotonic()
    with PressTrace(surface) as trace:
        summary, error = await press()
    await pause(0.5)
    with RUN.verifying():
        came_forward = front_pid(backend) != finder_pid
        lines = (await asyncio.to_thread(fixture.lines))[before:] if fixture is not None else []
    return trace, summary, error, came_forward, lines, started


def trial_from(name: str, trace: PressTrace, summary: str, error: str, came_forward: bool, lines: list[str],
               started: float, *, logs: bool) -> Trial:
    """A trial's result, unless the surface did something other than a plain AXPress - then it is not a
    measurement of AXPress and says so."""
    note = ""
    if error:
        note = f"the press raised: {error}"
    elif trace.clicks:
        note = "AXPress did not do it; the surface fell back to a click"
    elif trace.axpress is not True:
        note = "AXPress was not accepted"
    pressed = any(line.startswith(("click:", "toggle:")) for line in lines)
    return Trial(name, accepted=trace.axpress is True and not trace.clicks and not error,
                 recorded=pressed if logs else None, came_forward=came_forward, note=note,
                 **(activation_facts(lines, started) if logs else {}))


async def jarvis_trial(surface: NativeSurface, fixture, finder_pid: int, label: str, name: str, *, logs: bool) -> Trial:
    snap, _ = await surface.read(FIXTURE)
    control = next((c for c in snap.controls if c.label == label), None)
    if control is None:
        return Trial(name, note=f"the window has no “{label}” button: " + names(snap.controls))
    return trial_from(name, *await front_and_press(surface, finder_pid, lambda: try_press(surface, control.handle),
                                                   fixture if logs else None), logs=logs)


def raw_press_process(pid: int, identifier: str) -> tuple[str, str]:
    """RAW_PRESS in a process of its own: (what it printed, an error or "")."""
    done = subprocess.run([sys.executable, "-c", RAW_PRESS, str(pid), identifier], capture_output=True, text=True,
                          timeout=20)
    said = (done.stdout + done.stderr).strip()
    return said, "" if done.returncode == 0 else said or f"exit {done.returncode}"


async def raw_trial(surface: NativeSurface, fixture, finder_pid: int, target_pid: int) -> Trial:
    name = "the Save button, pressed by a bare AXUIElementPerformAction in another process"

    async def press():
        return await asyncio.to_thread(raw_press_process, target_pid, "save")

    trace, summary, error, came_forward, lines, started = await front_and_press(surface, finder_pid, press, fixture)
    pressed = any(line.startswith(("click:", "toggle:")) for line in lines)
    return Trial(name, accepted=not error, recorded=pressed, came_forward=came_forward,
                 note=f"the other process could not press it: {error}" if error else "",
                 **activation_facts(lines, started))


async def calculator_trial(surface: NativeSurface, finder_pid: int) -> Trial:
    """A standard app's control, pressed the same way: Calculator's clear button, which changes nothing
    that matters. Launched here if it was not running, and quit again."""
    name = "Calculator's clear button, pressed by JARVIS"
    was_running = is_running("Calculator")
    try:
        launch("Calculator")
        await pause(1.5)
        snap, _ = await surface.read("Calculator")
        control = pick(snap.controls, *CALC_CLEAR)
        if control is None:
            return Trial(name, note="no clear button: " + names(snap.controls, 12))
        return trial_from(name, *await front_and_press(surface, finder_pid, lambda: try_press(surface, control.handle)),
                          logs=False)
    except Exception as exc:
        return Trial(name, note=describe_failure(exc))
    finally:
        if not was_running:
            quit_app("Calculator")


def diagnose_activation(trials: dict[str, Trial]) -> list[str]:
    """What the controlled trials say about who brings the app forward. *trials* holds up to four, by key:
    ``save`` (the press that did it), ``raw`` (the same press by a client with nothing of JARVIS in it),
    ``inert`` (a button with no action, pressed by JARVIS), ``calculator`` (a standard app's button).
    The reading is the one most consistent with them, not a proof."""
    lines = [trial.describe() for key in ("save", "raw", "inert", "calculator") if (trial := trials.get(key))]

    def forward(key: str) -> bool | None:
        trial = trials.get(key)
        return trial.came_forward if trial is not None and trial.usable else None

    save = trials.get("save")
    if save is not None and save.handler:
        if save.handler.get("active") == "1":
            lines.append("when the button's action began the app was already active: the activation came before any "
                         "fixture code ran, from AppKit or the accessibility server")
        else:
            lines.append("when the button's action began the app was not yet active: it became active afterwards "
                         "(the action itself only writes a log line)")
    raw, inert, calculator = forward("raw"), forward("inert"), forward("calculator")
    if save is not None and save.came_forward and save.self_reported is False:
        lines.append("most consistent with the measurement (cause 5): Accessibility names the fixture as the focused "
                     "application, but the app itself never logged becoming active - nothing was brought forward, and "
                     "AXFocusedApplication is what moved (a press can change that without activating the app)")
        return lines
    if raw is None:
        lines.append("undecided: the press from a client with nothing of JARVIS in it could not be made, so JARVIS's "
                     "own part is not separated from macOS's")
    elif raw is False:
        lines.append("most consistent with JARVIS's invocation (cause 4): the same press from a bare client leaves "
                     "the app behind, so something JARVIS does around the press, not the action itself, brings it forward")
    elif inert is False:
        lines.append("most consistent with the fixture's handling of the press (cause 1): a button with no action is "
                     "pressed without the app coming forward, so it is running the action that does it")
    elif inert is None:
        lines.append("narrowed to the app or macOS, not JARVIS (a bare client does it too); the no-action button was "
                     "not measured, so the fixture's own handler is not ruled in or out")
    elif calculator is False:
        lines.append("most consistent with this fixture and its host process (causes 1/5), not AXPress in general: "
                     "a bare client brings it forward, a button with no action does too, and a standard app's button "
                     "does not")
    elif calculator is True:
        lines.append("most consistent with macOS/AppKit (cause 3): a bare client, a button with no action and a "
                     "standard app's button all bring their app forward on AXPress")
    else:
        lines.append("narrowed to the app or macOS, not JARVIS or the fixture's handler; Calculator was not "
                     "measured, so a fixture-only cause is not ruled out")
    return lines


async def localise_activation(surface: NativeSurface, fixture, finder_pid: int, target_pid: int,
                              primary: Trial) -> list[str]:
    """Run the controlled trials that follow a press which activated the app, and read them."""
    trials = {"save": primary}
    watching = not any(line.startswith("note:no activation log") for line in await asyncio.to_thread(fixture.lines))
    primary.watching = watching
    for key, trial in (("raw", lambda: raw_trial(surface, fixture, finder_pid, target_pid)),
                       ("inert", lambda: jarvis_trial(surface, fixture, finder_pid, "Inert",
                                                      "the Inert button (no action), pressed by JARVIS", logs=False)),
                       ("calculator", lambda: calculator_trial(surface, finder_pid))):
        try:
            trials[key] = await trial()
            trials[key].watching = watching
        except Exception as exc:
            trials[key] = Trial(key, note=describe_failure(exc))
    return diagnose_activation(trials)


async def stale_checks(surface: NativeSurface, fixture: FixtureProcess) -> bool:
    ok = await guarded("press without focus", timed("stale: press without focus", background_press,
                                                    surface, fixture))
    for scenario, command, label in STALE_SCENARIOS:
        name = f"stale: {scenario}"
        outcome = await guarded(label, timed(name, stale_scenario, surface, fixture, command))
        if outcome is False:
            ok = False
        elif isinstance(outcome, str):
            ok &= step(label, False, outcome)
            RUN.mark(name, False)
        else:
            status, why = judge(scenario, outcome)
            ok &= step(label, status == "ok", why, inconclusive=status == "inconclusive")
            RUN.mark(name, status == "ok")
    return ok


async def stale(surface: NativeSurface) -> bool:
    print("Stale handles — a window that rebuilds its controls on command")
    with tempfile.TemporaryDirectory(prefix="jarvis-fixture-") as directory:
        fixture = FixtureProcess(Path(directory))
        try:
            pid = await asyncio.to_thread(fixture.start)
        except RuntimeError as exc:
            return step("the fixture window opened", False, str(exc))
        backend = surface.backend
        original = backend.find_app
        # The window belongs to a Python process, whose name is no use for finding it.
        backend.find_app = lambda name: (pid, FIXTURE) if name.strip().lower() == FIXTURE.lower() else original(name)
        try:
            return await stale_checks(surface, fixture)
        finally:
            backend.find_app = original
            await asyncio.to_thread(fixture.stop)


# --- --observe: the Accessibility observer thread ----------------------------------------------
#
# automation.native_observer lets a wait on an app (it coming to the front, a menu
# opening) end the moment the app posts the matching notification instead of on the
# next 50 ms poll. The polling underneath is unchanged either way, so this is about
# whether the extra is real: do the notifications arrive, how much sooner, and does
# the thread stay out of the event loop's way. Nothing here changes any setting.

#: How much longer than the baseline the event loop may stall while the observer
#: thread is spinning before that counts as the thread holding things up.
LOOP_LAG_ALLOWED_S = 0.025
#: How much slower than polling alone bringing an app to the front may get.
FRONT_SLOWER_ALLOWED_S = 0.05


def front_pid(backend) -> int | None:
    front = backend.frontmost()
    return front[0] if front else None


def wait_front(backend, pid: int, timeout_s: float = 2.0) -> float | None:
    """Seconds until *pid* is the frontmost app, polling every 5 ms; None if it never is."""
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        if front_pid(backend) == pid:
            return time.monotonic() - start
        time.sleep(0.005)
    return None


def time_activation(surface: NativeSurface, away: int, to: int) -> dict:
    """Bring *to* to the front from *away*, and say when the notification
    arrived and when polling noticed the change (seconds after asking)."""
    backend = surface.backend
    backend.activate(away)
    wait_front(backend, away)
    with surface.watch(to, ACTIVATED) as wake:
        if wake is None:
            return {"watched": False}
        start = time.monotonic()
        backend.activate(to)
        seen = wait_front(backend, to)
        wake.wait(0.5)                                  # a moment for the notification to catch up
        fired = None if wake.fired_at is None else wake.fired_at - start
    return {"watched": True, "fired": fired, "seen": seen}


def time_menu(surface: NativeSurface, pid: int, title: str) -> dict:
    """Open the *title* menu of *pid*'s menu bar and say when the notification
    arrived and when its items showed up (seconds after pressing; none if they
    were there already, which is the usual case for a menu bar menu)."""
    backend = surface.backend
    bar = backend.attribute(backend.application(pid), "AXMenuBar")
    item = next((i for i in backend.attribute(bar, "AXChildren") or []
                 if backend.attribute(i, "AXTitle") == title), None)
    if item is None:
        return {"watched": True, "missing": title}

    def filled() -> bool:
        return any(backend.attribute(menu, "AXChildren")
                   for menu in backend.attribute(item, "AXChildren") or [])

    prefilled = filled()                 # most menu bar menus already hold their items; nothing to wait for
    with surface.watch(pid, MENU_OPENED) as wake:
        if wake is None:
            return {"watched": False}
        start = time.monotonic()
        backend.perform(item, "AXPress")
        seen = None
        while not prefilled and time.monotonic() - start < 2.0 and seen is None:
            if filled():
                seen = time.monotonic() - start
            else:
                time.sleep(0.005)
        wake.wait(0.5)
        fired = None if wake.fired_at is None else wake.fired_at - start
    surface.input.press(resolve_key("escape"))
    return {"watched": True, "fired": fired, "seen": seen, "prefilled": prefilled}


def cycle_watches(surface: NativeSurface, pid: int, count: int = 25) -> tuple[int, int, float]:
    """Subscribe and unsubscribe *count* times; (made, threads before, seconds)."""
    threads = threading.active_count()
    start = time.monotonic()
    made = 0
    for _ in range(count):
        with surface.watch(pid, ACTIVATED) as wake:
            made += wake is not None
    return made, threads, time.monotonic() - start


def average_front(surface: NativeSurface, away: int, to: int, count: int = 5) -> tuple[float, int]:
    """The surface's own wait on an app coming to the front, *count* times:
    (mean seconds, how many times the app really was in front afterwards)."""
    backend, total, arrived = surface.backend, 0.0, 0
    for _ in range(count):
        backend.activate(away)
        wait_front(backend, away)
        start = time.monotonic()
        surface._front(to)
        total += time.monotonic() - start
        arrived += front_pid(backend) == to
    return total / count, arrived


#: A breath between two timed waits, so one doesn't run into the next.
ROUND_GAP_S = 0.15


def repeat_timing(measure, count: int, *args) -> list[dict]:
    """*measure*(*args*) *count* times, with a breath between."""
    results = []
    for _ in range(count):
        results.append(measure(*args))
        time.sleep(ROUND_GAP_S)
    return results


def estimated_saving(round_: dict) -> float | None:
    """How much sooner than a 50 ms poll the notification would have ended one
    wait: the poll sees the change at its next tick after it happened; the
    observer, when the notification arrives (never before the change)."""
    if not round_.get("watched") or round_.get("fired") is None or round_.get("seen") is None:
        return None
    poll_sees = -(-round_["seen"] // 0.05) * 0.05
    return poll_sees - max(round_["fired"], round_["seen"])


def median(values: list[float]) -> float:
    return nv.percentile(values, 0.5)


async def event_loop_lag(seconds: float) -> float:
    """The longest the event loop was kept from a 10 ms tick over *seconds*."""
    worst, end = 0.0, time.monotonic() + seconds
    while time.monotonic() < end:
        before = time.monotonic()
        await asyncio.sleep(0.01)
        worst = max(worst, time.monotonic() - before - 0.01)
    return worst


def ms(seconds: float | None) -> str:
    return "never" if seconds is None else f"{seconds * 1000:.0f} ms"


async def bounded(function, *args, timeout_s: float = 60.0):
    """Run a blocking call on a worker thread, and say so rather than hang if it never returns."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(function, *args), timeout_s)
    except TimeoutError:
        raise RuntimeError(f"{getattr(function, '__name__', 'a call')} didn't finish within "
                           f"{timeout_s:.0f} s — a deadlock?") from None


async def observer_cpu(surface: NativeSurface, pid: int, seconds: float | None = None) -> float | None:
    """The share of one core this process uses over *seconds* with a live
    subscription and nothing else going on; None if none could be made."""
    entered = surface.watch(pid, ACTIVATED)
    wake = await asyncio.to_thread(entered.__enter__)
    try:
        if wake is None:
            return None
        cpu, wall = time.process_time(), time.monotonic()
        await asyncio.sleep(CPU_SECONDS if seconds is None else seconds)
        return (time.process_time() - cpu) / (time.monotonic() - wall)
    finally:
        await asyncio.to_thread(entered.__exit__, None, None, None)


#: What the observer thread may cost, as a share of one core, while merely waiting.
CPU_ALLOWED = 0.05
CPU_SECONDS = 3.0
#: How many times each wait is timed.
ROUNDS = 15


def observer_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "jarvis-ax-observer"]


async def observe(on: NativeSurface, off: NativeSurface) -> bool:
    """*on* has the observer enabled; *off* is the same surface without it.

    Two questions, kept apart. Is it correct: do the notifications arrive, every
    time, without a callback raising, a thread left behind, the CPU running or
    the event loop stalling, and do waits still work when it isn't there? And is
    it useful: how much sooner than polling does it end a wait? The second is a
    measurement, not a pass or a fail."""
    print("AXObserver — what this Mac does")
    was_running = is_running("Calculator")
    launch("Calculator")
    await pause(1.5)
    with RUN.measure("observer", on):
        try:
            return await _observe(on, off)
        finally:
            on.close()
            if not was_running:
                quit_app("Calculator")


async def _observe(on: NativeSurface, off: NativeSurface) -> bool:
    backend = on.backend
    already = observer_threads()
    calc, finder = backend.find_app("Calculator"), backend.find_app("Finder")
    if not step("found Calculator and Finder", calc is not None and finder is not None):
        return False
    calc_pid, finder_pid = calc[0], finder[0]
    ok = True

    activations = await bounded(repeat_timing, time_activation, ROUNDS, on, finder_pid, calc_pid)
    menus = await bounded(repeat_timing, time_menu, ROUNDS, on, calc_pid, "View")
    for label, rounds, what in (("AXApplicationActivated arrives", activations, "the app in front"),
                                ("AXMenuOpened arrives", menus, "the items")):
        if not rounds[0].get("watched"):
            ok &= step(label, False,
                       "no subscription was made — the observer thread or the call failed (JARVIS would just poll)")
        elif rounds[0].get("missing"):
            ok &= step("Calculator has a View menu", False, f"no “{rounds[0]['missing']}” in its menu bar")
        else:
            fired = [r["fired"] for r in rounds if r.get("fired") is not None]
            seen = [r["seen"] for r in rounds if r.get("seen") is not None]
            polled = (f"polling saw {what} at {ms(median(seen))}" if seen
                      else "polling had nothing to wait for — the menu was already filled in")
            ok &= step(label, bool(fired), f"median {ms(median(fired) if fired else None)}; {polled}")
    watched = [r for r in (*activations, *menus) if r.get("watched") and not r.get("missing")]
    missed = sum(1 for r in watched if r.get("fired") is None)
    ok &= step("no notification was missed", bool(watched) and missed == 0,
               f"{missed} of {len(watched)} waits got no notification" if watched else "nothing was watched")

    def bogus():
        start = time.monotonic()
        with on.watch(calc_pid, "AXNoSuchNotification") as wake:
            return wake is not None, time.monotonic() - start
    accepted, took = await bounded(bogus)
    ok &= step("an unsupported notification is handled", took < 1.0,
               ("macOS accepted it, so it just never fires" if accepted else "refused, so JARVIS polls")
               + f" ({ms(took)})")

    made, threads, took = await bounded(cycle_watches, on, calc_pid)
    ok &= step("25 subscribe/unsubscribe cycles leave nothing behind",
               made == 25 and threading.active_count() <= threads + 1,
               f"{made} of 25 subscribed in {ms(took)}; threads {threads} → {threading.active_count()}")

    stats = on.observer_stats()
    ok &= step("no callback raised", bool(stats) and stats["callback_errors"] == 0 and not stats["broken"],
               f"observer thread stats: {stats}" if stats else "there is no observer thread")

    cpu = await observer_cpu(on, calc_pid)
    ok &= step("the observer costs next to no CPU", cpu is not None and cpu <= CPU_ALLOWED,
               "no subscription, so nothing was measured" if cpu is None
               else f"{cpu * 100:.1f}% of one core while waiting (limit {CPU_ALLOWED * 100:.0f}%)")

    baseline = await event_loop_lag(1.0)
    entered = on.watch(calc_pid, ACTIVATED)
    wake = await asyncio.to_thread(entered.__enter__)
    try:
        spinning = await event_loop_lag(2.0)
    finally:
        await asyncio.to_thread(entered.__exit__, None, None, None)
    if wake is None:
        ok &= step("the event loop stays free while the observer spins", False,
                   "no subscription, so nothing was spinning to measure")
    else:
        ok &= step("the event loop stays free while the observer spins",
                   spinning <= baseline + LOOP_LAG_ALLOWED_S,
                   f"worst stall {ms(spinning)} with it, {ms(baseline)} without")

    with_it, arrived_with = await bounded(average_front, on, finder_pid, calc_pid, 10)
    without, arrived_without = await bounded(average_front, off, finder_pid, calc_pid, 10)
    ok &= step("bringing an app to the front is no slower with it",
               with_it <= without + FRONT_SLOWER_ALLOWED_S and arrived_with >= arrived_without,
               f"{ms(with_it)} with, {ms(without)} without, mean of 10; the app was in front afterwards "
               f"{arrived_with}/10 and {arrived_without}/10 times")

    savings = [saving for round_ in (*activations, *menus) if (saving := estimated_saving(round_)) is not None]
    if savings:
        typical = median(savings)
        verdict = ("no measurable gain — leave it off" if typical < 0.010 else
                   "a small latency optimisation, not a reliability feature" if typical < 0.060 else
                   "a real latency gain")
        RUN.extra["observer_usefulness"] = {"waits": len(savings), "median_saving_ms": typical * 1000,
                                            "best_ms": max(savings) * 1000, "worst_ms": min(savings) * 1000,
                                            "verdict": verdict}
        print(f"  ℹ usefulness: a 50 ms poll would have ended these waits a median {ms(typical)} later "
              f"(best {ms(max(savings))}, worst {ms(min(savings))}, {len(savings)} waits) — {verdict}")
    else:
        print("  ℹ usefulness: no wait produced both a notification and a poll to compare")

    # Last: stop it, and check nothing is left, and that waits go on working without it.
    await asyncio.to_thread(on.close)
    left = [thread.name for thread in observer_threads() if thread not in already and thread.is_alive()]
    ok &= step("no thread is left behind", not left,
               f"still running: {left}" if left else "the observer thread has ended")
    after, arrived_after = await bounded(average_front, on, finder_pid, calc_pid, 3)
    ok &= step("waits still work with the observer stopped", arrived_after == 3,
               f"the app was in front afterwards {arrived_after}/3 times, mean {ms(after)}")
    return ok


async def run_once(args, surface: NativeSurface) -> bool:
    ok = await guarded("look", timed("look", look, surface, args.app))
    if args.act:
        ok &= await guarded("TextEdit round trip", timed("textedit round trip", act, surface))
    if args.controls:
        ok &= await controls(surface)
    if args.stale:
        ok &= await stale(surface)
    if args.observe:
        ok &= await guarded("AXObserver", observe(NativeSurface(observe=True), NativeSurface(observe=False)))
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--app", default="", help="an app to look at (default: the frontmost)")
    parser.add_argument("--act", action="store_true", help="also run the TextEdit round trip")
    parser.add_argument("--controls", action="store_true",
                        help="also click, choose from a pop-up, drag and click marks (Calculator, "
                             "TextEdit's Save sheet, a throwaway Desktop folder)")
    parser.add_argument("--stale", action="store_true",
                        help="also check re-finding a rebuilt control, and refusing look-alikes, "
                             "in a window of its own (scripts/ax_fixture.py)")
    parser.add_argument("--observe", action="store_true",
                        help="also measure the Accessibility observer thread (automation.native_observer)")
    parser.add_argument("--all", action="store_true",
                        help="everything above, in that order, then the exit-criteria table; "
                             "writes a results file")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="run it all N times: each check's pass rate and timings over the runs "
                             f"(the Repeatability row needs {nv.MIN_REPEATS} or more)")
    parser.add_argument("--json", type=Path, metavar="PATH",
                        help="write every result, timing and criterion here (default with --all: "
                             "native-validation-<time>.json)")
    args = parser.parse_args()
    if args.all:
        args.act = args.controls = args.stale = args.observe = True
        args.json = args.json or Path(f"native-validation-{datetime.now():%Y%m%d-%H%M%S}.json")
    args.repeat = max(1, args.repeat)
    print(nv.format_revision(nv.revision(ROOT)))
    surface = NativeSurface()
    if not surface.available():
        print("The native extras aren't installed here: pip install -e '.[native]' (macOS only).")
        return 1
    if not args.app:
        args.app = host_app(surface.backend)       # the same window every time, whatever ran before
        if args.app:
            print(f"Starting point: {args.app}, the app this was run from (--app NAME to look at another).")
    if not step("Accessibility granted", surface.backend.trusted()):
        surface.backend.trusted(prompt=True)
        print("  Allow it in System Settings → Privacy & Security → Accessibility, then run again.")
        return 1
    if args.all or args.json or args.repeat > 1:
        env = nv.environment()
        print(f"{env['chip'] or env['machine']} · macOS {env['macos']} · Python {env['python']}")
    ok = True
    for number in range(1, args.repeat + 1):
        RUN.iteration = number
        if args.repeat > 1:
            print(f"\n=== run {number} of {args.repeat} ===")
        ok &= await run_once(args, surface)
    summary = nv.summarise(RUN.runs)
    if len(summary) > 1 or args.repeat > 1:
        print("\n" + "\n".join(nv.format_timings(summary, args.repeat)))
    if args.all:
        print("\n" + "\n".join(nv.format_criteria(nv.evaluate(RUN.steps, RUN.runs, args.repeat))))
    if args.json:
        report = nv.build_report(RUN, argv=sys.argv[1:], repeat=args.repeat, ok=ok)
        args.json.write_text(json.dumps(report, indent=2))
        print(f"\nResults written to {args.json} — send that file back (it lists the labels the windows showed).")
    print("\nAll good." if ok else "\nSomething didn't work — the lines marked ✗ (or ⚠) say what.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
