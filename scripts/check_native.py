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
    .venv/bin/python scripts/check_native.py --observe  # do AXObserver notifications fire?

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
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.surfaces.native import NativeError, NativeSurface  # noqa: E402
from jarvis.surfaces.native.input import resolve_key  # noqa: E402
from jarvis.surfaces.native.marks import MIN_CONFIDENCE, image_size, recognize_text, to_points  # noqa: E402
from jarvis.surfaces.native.surface import ACTIVATED, MENU_OPENED  # noqa: E402


def step(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    return ok


async def capture_window(pid: int, number: int) -> Path:
    path = Path(tempfile.mkdtemp()) / "window.png"
    result = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", f"-l{number}", str(path)],
                            capture_output=True, text=True)
    if result.returncode != 0 or not path.exists():
        raise NativeError("screencapture failed — is Screen Recording allowed?")
    return path


async def look(surface: NativeSurface, app: str) -> bool:
    print(f"Looking at {app or 'the frontmost app'}")
    try:
        snap, listing = await surface.read(app)
    except NativeError as exc:
        return step("read the window", False, exc.message)
    step("read the window", True, f"{snap.total} controls, {snap.visited} elements visited")
    print("\n" + "\n".join("    " + line for line in listing.splitlines()[:40]) + "\n")
    ok = step("menus", bool(snap.menus), ", ".join(snap.menus[:8]))

    try:
        marks, marked, overlay = await surface.mark(capture_window, app,
                                                    overlay_dir=Path(tempfile.mkdtemp()))
        texts = [m for m in marks if m.source == "ocr"]
        ok &= step("text on a screenshot", bool(texts), f"{len(texts)} pieces of text, "
                   f"{len(marks)} marks" + (f", overlay at {overlay}" if overlay else ""))
    except Exception as exc:  # report, don't crash
        ok &= step("text on a screenshot", False, str(exc))
    return ok


async def act(surface: NativeSurface) -> bool:
    print("TextEdit round trip")
    subprocess.run(["open", "-a", "TextEdit"], check=False)
    time.sleep(2.0)
    try:
        await surface.choose_menu(["File", "New"], "TextEdit")
        time.sleep(1.0)
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
        time.sleep(0.8)
        snap, listing = await surface.read("TextEdit")
        dont_save = next((c for c in snap.controls if "don" in c.label.lower() and "save" in c.label.lower()),
                         None)
        ok &= step("the save sheet appeared, listed first", dont_save is not None
                   and snap.controls.index(dont_save) < 4)
        if dont_save is not None:
            await surface.press(dont_save.handle)
        return ok
    except NativeError as exc:
        return step("TextEdit round trip", False, exc.message)


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
    await asyncio.sleep(seconds)


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
    """Whether *text* is what some piece of the window says, by itself."""
    def clean(value: str) -> str:
        return value.strip().strip("\u200e\u200f\u202a\u202c").strip()
    return (any(clean(t) == text for t in snap.texts)
            or any(text in (clean(c.value), clean(c.label)) for c in snap.controls))


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
    return step("read_window shows the result", shows(snap, "12"),
                f"texts: {snap.texts[:6]}; controls: {names(snap.controls, 14)}") and ok


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
    snap, _ = await surface.read("Calculator")
    path, boxes = seen[-1]
    size = image_size(path)
    compared, off = 0, []
    for box in boxes:
        said = box.text.strip().lower()
        control = next((c for c in snap.controls if c.frame is not None
                        and c.label.strip().lower() == said), None)
        if box.confidence < MIN_CONFIDENCE or control is None or snap.frame is None:
            continue
        compared += 1
        x, y = to_points(box, size, snap.frame).center
        f = control.frame
        if not (f.x - 2 <= x <= f.x + f.w + 2 and f.y - 2 <= y <= f.y + f.h + 2):
            off.append(f'“{box.text}” read at ({x:.0f}, {y:.0f}), the control is at '
                       f'({f.x:.0f}, {f.y:.0f}, {f.w:.0f}×{f.h:.0f})')
    ok &= step("text read off the screenshot lands on the controls it names",
               compared > 0 and not off,
               f"{compared - len(off)} of {compared} agree" + (f"; {'; '.join(off[:3])}" if off else "")
               if compared else f"no text matched a control's name ({len(boxes)} pieces of text read)")

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
        ok = await guarded("click_control in Calculator", calculator_click(surface))
        ok &= await guarded("mark_screen in Calculator", calculator_marks(surface))
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
        summary = await surface.choose_option(popup.handle, "Desktop")
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
        await pause(1.5)
        moved = (target / "drag-me.txt").exists() and not source.exists()
        return step("the file is inside the folder", moved,
                    summary + ("" if moved else f" On disk: {sorted(p.name for p in base.rglob('*'))}"))
    finally:
        with contextlib.suppress(NativeError):
            front, _ = await surface.read("Finder")
            if front.title == base.name:        # only the window this check opened
                await surface.choose_menu(["File", "Close Window"], "Finder")
        shutil.rmtree(base, ignore_errors=True)


async def controls(surface: NativeSurface) -> bool:
    ok = await calculator(surface)
    ok &= await guarded("choose_option in TextEdit", textedit_dropdown(surface))
    ok &= await guarded("drag_control in Finder", finder_drag(surface))
    return ok


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
    arrived and when its items showed up (seconds after pressing)."""
    backend = surface.backend
    bar = backend.attribute(backend.application(pid), "AXMenuBar")
    item = next((i for i in backend.attribute(bar, "AXChildren") or []
                 if backend.attribute(i, "AXTitle") == title), None)
    if item is None:
        return {"watched": True, "missing": title}

    def filled() -> bool:
        return any(backend.attribute(menu, "AXChildren")
                   for menu in backend.attribute(item, "AXChildren") or [])

    with surface.watch(pid, MENU_OPENED) as wake:
        if wake is None:
            return {"watched": False}
        start = time.monotonic()
        backend.perform(item, "AXPress")
        seen = None
        while time.monotonic() - start < 2.0 and seen is None:
            if filled():
                seen = time.monotonic() - start
            else:
                time.sleep(0.005)
        wake.wait(0.5)
        fired = None if wake.fired_at is None else wake.fired_at - start
    surface.input.press(resolve_key("escape"))
    return {"watched": True, "fired": fired, "seen": seen}


def cycle_watches(surface: NativeSurface, pid: int, count: int = 25) -> tuple[int, int, float]:
    """Subscribe and unsubscribe *count* times; (made, threads before, seconds)."""
    threads = threading.active_count()
    start = time.monotonic()
    made = 0
    for _ in range(count):
        with surface.watch(pid, ACTIVATED) as wake:
            made += wake is not None
    return made, threads, time.monotonic() - start


def average_front(surface: NativeSurface, away: int, to: int, count: int = 5) -> float:
    """Mean seconds for the surface's own wait on an app coming to the front."""
    backend, total = surface.backend, 0.0
    for _ in range(count):
        backend.activate(away)
        wait_front(backend, away)
        start = time.monotonic()
        surface._front(to)
        total += time.monotonic() - start
    return total / count


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


async def observe(on: NativeSurface, off: NativeSurface) -> bool:
    """*on* has the observer enabled; *off* is the same surface without it."""
    print("AXObserver — what this Mac does")
    was_running = is_running("Calculator")
    launch("Calculator")
    await pause(1.5)
    try:
        backend = on.backend
        calc, finder = backend.find_app("Calculator"), backend.find_app("Finder")
        if not step("found Calculator and Finder", calc is not None and finder is not None):
            return False
        calc_pid, finder_pid = calc[0], finder[0]
        ok = True

        found = await asyncio.to_thread(time_activation, on, finder_pid, calc_pid)
        if found["watched"]:
            ok &= step("AXApplicationActivated arrives", found["fired"] is not None,
                       f"notification at {ms(found['fired'])}; polling saw the app in front at {ms(found['seen'])}")
        else:
            ok &= step("subscribed to AXApplicationActivated", False,
                       "no subscription was made — the observer thread or the call failed (JARVIS would just poll)")

        found = await asyncio.to_thread(time_menu, on, calc_pid, "View")
        if found.get("missing"):
            ok &= step("Calculator has a View menu", False, f"no “{found['missing']}” in its menu bar")
        elif found["watched"]:
            ok &= step("AXMenuOpened arrives", found["fired"] is not None,
                       f"notification at {ms(found['fired'])}; polling saw the items at {ms(found['seen'])}")
        else:
            ok &= step("subscribed to AXMenuOpened", False, "no subscription was made (JARVIS would just poll)")

        def bogus():
            start = time.monotonic()
            with on.watch(calc_pid, "AXNoSuchNotification") as wake:
                return wake is not None, time.monotonic() - start
        accepted, took = await asyncio.to_thread(bogus)
        ok &= step("an unsupported notification is handled", took < 1.0,
                   ("macOS accepted it, so it just never fires" if accepted else "refused, so JARVIS polls")
                   + f" ({ms(took)})")

        made, threads, took = await asyncio.to_thread(cycle_watches, on, calc_pid)
        ok &= step("25 subscribe/unsubscribe cycles leave nothing behind",
                   made == 25 and threading.active_count() <= threads + 1,
                   f"{made} of 25 subscribed in {ms(took)}; threads {threads} → {threading.active_count()}")

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

        with_it = await asyncio.to_thread(average_front, on, finder_pid, calc_pid)
        without = await asyncio.to_thread(average_front, off, finder_pid, calc_pid)
        ok &= step("bringing an app to the front is no slower with it", with_it <= without + FRONT_SLOWER_ALLOWED_S,
                   f"{ms(with_it)} with, {ms(without)} without, mean of 5")
        return ok
    finally:
        on.close()
        if not was_running:
            quit_app("Calculator")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--app", default="", help="an app to look at (default: the frontmost)")
    parser.add_argument("--act", action="store_true", help="also run the TextEdit round trip")
    parser.add_argument("--observe", action="store_true",
                        help="also measure the Accessibility observer thread (automation.native_observer)")
    parser.add_argument("--controls", action="store_true",
                        help="also click, choose from a pop-up, drag and click marks (Calculator, "
                             "TextEdit's Save sheet, a throwaway Desktop folder)")
    args = parser.parse_args()
    surface = NativeSurface()
    if not surface.available():
        print("The native extras aren't installed here: pip install -e '.[native]' (macOS only).")
        return 1
    if not step("Accessibility granted", surface.backend.trusted()):
        surface.backend.trusted(prompt=True)
        print("  Allow it in System Settings → Privacy & Security → Accessibility, then run again.")
        return 1
    ok = await look(surface, args.app)
    if args.act:
        ok &= await act(surface)
    if args.controls:
        ok &= await controls(surface)
    if args.observe:
        ok &= await observe(NativeSurface(observe=True), NativeSurface(observe=False))
    print("\nAll good." if ok else "\nSomething didn't work — the lines marked ✗ say what.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
