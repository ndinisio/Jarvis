"""Build the --stale fixture as a real macOS application bundle.

``scripts/ax_fixture.py`` used to be started as ``python ax_fixture.py`` by the harness: a child of the harness
(itself a child of Terminal) that LaunchServices never launched, with the identity of the interpreter's own
``Python.app`` - the same one every other Python process has. A background-press check needs an application
that AppKit and LaunchServices treat like any other: launched by LaunchServices (``open``), from a bundle of its
own, with an identifier and a name of its own. This builds one, in a directory the caller owns, with nothing
but the standard library, the interpreter that is already running, and ``codesign`` (which every Mac has):

    <directory>/JARVIS Fixture.app/
        Contents/Info.plist             the interpreter's Python.app plist, re-identified (see below)
        Contents/MacOS/Python           a copy of the interpreter's own GUI executable
        Contents/Resources/ax_fixture.py   the fixture, so the bundle is what runs it

It is the interpreter's own ``Resources/Python.app`` (python.org and Homebrew builds have one) copied and
re-identified, not a new executable: the code that runs is exactly what ran before, under another identity. The
plist gets a bundle identifier and name of its own and an ``LSEnvironment`` that carries the running
interpreter's ``site-packages`` (PyObjC) onto ``PYTHONPATH``; every key that registers the copy as a handler for
something (document types, URL schemes, scripting) is dropped, so a throwaway copy cannot take over how a Mac opens
``.py`` files. The bundle is signed ad hoc, best effort - no certificate, no notarisation.

What this does not do: it does not make anything inactive (nothing here knows what is active), it uses no private
API, and it cannot work for an interpreter with no ``Python.app`` (a non-framework build such as a conda or
python-build-standalone one): that raises :class:`BundleError` and says so. Whether the copy actually launches is
the one thing only a Mac can say; the harness reports what the launch printed when it does not.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

IDENTIFIER = "local.jarvis.ax-fixture"
NAME = "JARVIS Fixture"

#: Plist keys that would register the copy with LaunchServices as a handler for something, or describe the
#: original's signature or document types. None of them is wanted on a throwaway copy.
DROPPED_KEYS = ("CFBundleDocumentTypes", "CFBundleURLTypes", "UTExportedTypeDeclarations", "UTImportedTypeDeclarations",
                "NSAppleScriptEnabled", "OSAScriptingDefinition", "CFBundleSignature", "NSServices", "CFBundleHelpBookFolder",
                "CFBundleHelpBookName")


class BundleError(RuntimeError):
    """The fixture could not be made into an application bundle, and why."""


class Bundle(NamedTuple):
    path: Path             # the .app
    identifier: str
    executable: Path       # Contents/MacOS/<CFBundleExecutable>
    script: Path           # the fixture inside the bundle
    source: Path           # the Python.app it was made from
    signing: str           # "ad hoc" or why it was not signed


def find_python_app(prefix: str | Path | None = None) -> Path:
    """The interpreter's own application bundle: ``<prefix>/Resources/Python.app``, which framework builds
    (python.org, Homebrew) have. *prefix* defaults to the running interpreter's ``sys.base_prefix``."""
    base = Path(prefix if prefix is not None else sys.base_prefix)
    found = base / "Resources" / "Python.app"
    if not (found / "Contents" / "Info.plist").is_file():
        raise BundleError(
            f"this Python ({base}) has no Resources/Python.app, so it is not a macOS framework build and the fixture "
            "cannot be made into an application from it. Run the check with a python.org or Homebrew Python.")
    return found


def site_packages(paths: list[str] | None = None) -> list[str]:
    """The ``site-packages`` directories the interpreter is running with (a virtual environment's, where
    PyObjC is), for ``PYTHONPATH`` in the copy: it cannot find the environment's ``pyvenv.cfg`` from inside the bundle."""
    seen: list[str] = []
    for entry in (sys.path if paths is None else paths):
        if entry and "site-packages" in Path(entry).parts and Path(entry).is_dir() and entry not in seen:
            seen.append(entry)
    return seen


def build(directory: Path, script: Path, *, prefix: str | Path | None = None, packages: list[str] | None = None,
          sign: bool = True, run: Callable = subprocess.run) -> Bundle:
    """``<directory>/JARVIS Fixture.app``, made from the interpreter's ``Python.app`` and holding *script*. Raises
    :class:`BundleError` if it cannot be made. *run* is how ``codesign`` is run (a parameter so that tests need no Mac)."""
    source = find_python_app(prefix)
    app = Path(directory) / f"{NAME}.app"
    try:
        if app.exists():
            shutil.rmtree(app)
        shutil.copytree(source, app, symlinks=True)
        shutil.rmtree(app / "Contents" / "_CodeSignature", ignore_errors=True)
        info_path = app / "Contents" / "Info.plist"
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        for key in DROPPED_KEYS:
            info.pop(key, None)
        info.update({"CFBundleIdentifier": IDENTIFIER, "CFBundleName": NAME, "CFBundleDisplayName": NAME,
                     "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
                     "LSEnvironment": {"PYTHONPATH": ":".join(site_packages() if packages is None else packages),
                                       "PYTHONDONTWRITEBYTECODE": "1"}})
        with info_path.open("wb") as handle:
            plistlib.dump(info, handle)
        executable = app / "Contents" / "MacOS" / str(info.get("CFBundleExecutable") or "Python")
        if not executable.is_file():
            raise BundleError(f"the copy of {source} has no executable at {executable}")
        resources = app / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        fixture = resources / Path(script).name
        shutil.copy2(script, fixture)
    except OSError as exc:
        raise BundleError(f"could not build {app}: {type(exc).__name__}: {exc}") from exc
    return Bundle(app, IDENTIFIER, executable, fixture, source, _sign(app, sign, run))


def _sign(app: Path, wanted: bool, run: Callable) -> str:
    """An ad hoc signature over the re-identified copy (its plist no longer matches the original's seal), if
    ``codesign`` is there to make one. Never fatal: what it says is recorded as evidence."""
    if not wanted:
        return "not signed (not asked)"
    try:
        done = run(["codesign", "--force", "--deep", "--sign", "-", str(app)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not signed ({type(exc).__name__}: {exc})"
    if done.returncode != 0:
        return f"not signed (codesign exit {done.returncode}: {(done.stderr or done.stdout or '').strip()[:200]})"
    return "ad hoc"
