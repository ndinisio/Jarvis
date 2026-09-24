#!/usr/bin/env python3
"""Check JARVIS's Mac app control on this Mac, for real.

The native surface (backend/jarvis/surfaces/native/) is tested in CI against
a fake accessibility tree; this runs the real thing. By default it only
looks: it lists the front window of an app, its menus, and reads text off a
screenshot of it. With --act it also opens TextEdit, types into a new
document, makes it bold from the Format menu, reads it back, and closes it
without saving.

    .venv/bin/python scripts/check_native.py            # look at the frontmost app
    .venv/bin/python scripts/check_native.py --app Notes
    .venv/bin/python scripts/check_native.py --act      # the TextEdit round trip

Needs the native extras (pip install -e '.[native]') and, for the process
running it (Terminal), Accessibility and — for the screenshot — Screen
Recording, in System Settings → Privacy & Security.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.surfaces.native import NativeError, NativeSurface  # noqa: E402
from jarvis.surfaces.native.input import resolve_key  # noqa: E402


def step(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    return ok


async def look(surface: NativeSurface, app: str) -> bool:
    print(f"Looking at {app or 'the frontmost app'}")
    try:
        snap, listing = await surface.read(app)
    except NativeError as exc:
        return step("read the window", False, exc.message)
    step("read the window", True, f"{snap.total} controls, {snap.visited} elements visited")
    print("\n" + "\n".join("    " + line for line in listing.splitlines()[:40]) + "\n")
    ok = step("menus", bool(snap.menus), ", ".join(snap.menus[:8]))

    async def capture(pid: int, number: int) -> Path:
        path = Path(tempfile.mkdtemp()) / "window.png"
        result = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", f"-l{number}", str(path)],
                                capture_output=True, text=True)
        if result.returncode != 0 or not path.exists():
            raise NativeError("screencapture failed — is Screen Recording allowed?")
        return path

    try:
        marks, marked, overlay = await surface.mark(capture, app, overlay_dir=Path(tempfile.mkdtemp()))
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


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--app", default="", help="an app to look at (default: the frontmost)")
    parser.add_argument("--act", action="store_true", help="also run the TextEdit round trip")
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
    print("\nAll good." if ok else "\nSomething didn't work — the lines marked ✗ say what.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
