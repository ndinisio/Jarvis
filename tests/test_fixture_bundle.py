"""The --stale fixture as an application bundle (scripts/fixture_bundle.py).

Nothing here needs a Mac: the interpreter's ``Python.app`` is a directory laid out the way a framework build's is,
and ``codesign`` is a recorder. What it proves is the shape of what is built - identity, environment, the keys that
are dropped, the original left alone - not that macOS will launch it; that is the real-Mac run's to say.
"""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def fb():
    spec = importlib.util.spec_from_file_location("fixture_bundle_script", SCRIPTS / "fixture_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORIGINAL = {
    "CFBundleIdentifier": "org.python.python", "CFBundleName": "Python", "CFBundleDisplayName": "Python",
    "CFBundleExecutable": "Python", "CFBundlePackageType": "APPL", "CFBundleSignature": "PytX",
    "CFBundleDocumentTypes": [{"CFBundleTypeExtensions": ["py"], "CFBundleTypeName": "Python Script"}],
    "CFBundleURLTypes": [{"CFBundleURLSchemes": ["python"]}], "NSAppleScriptEnabled": True,
    "UTExportedTypeDeclarations": [{"UTTypeIdentifier": "org.python.script"}], "LSMinimumSystemVersion": "11.0",
    "NSHighResolutionCapable": True,
}


@pytest.fixture
def interpreter(tmp_path):
    """A framework build's prefix: ``<prefix>/Resources/Python.app`` with a plist, an executable and a signature."""
    prefix = tmp_path / "Versions" / "3.14"
    app = prefix / "Resources" / "Python.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "_CodeSignature").mkdir()
    (app / "Contents" / "_CodeSignature" / "CodeResources").write_text("seal")
    (app / "Contents" / "MacOS" / "Python").write_text("#!mach-o")
    (app / "Contents" / "Resources" / "PythonInterpreter.icns").write_text("icon")
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(ORIGINAL))
    script = tmp_path / "ax_fixture.py"
    script.write_text("print('fixture')\n")
    return SimpleNamespace(prefix=prefix, app=app, script=script, out=tmp_path / "out")


class Codesign:
    def __init__(self, returncode=0, stderr="", error=None):
        self.calls, self.returncode, self.stderr, self.error = [], returncode, stderr, error

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.error:
            raise self.error
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


def make(fb, interpreter, **kwargs):
    interpreter.out.mkdir(exist_ok=True)
    kwargs.setdefault("run", Codesign())
    kwargs.setdefault("packages", ["/venv/lib/python3.14/site-packages"])
    return fb.build(interpreter.out, interpreter.script, prefix=interpreter.prefix, **kwargs)


def info(bundle):
    return plistlib.loads((bundle.path / "Contents" / "Info.plist").read_bytes())


def test_the_bundle_is_the_interpreters_own_application_under_an_identity_of_its_own(fb, interpreter):
    bundle = make(fb, interpreter)
    assert bundle.path == interpreter.out / "JARVIS Fixture.app" and bundle.identifier == "local.jarvis.ax-fixture"
    plist = info(bundle)
    assert plist["CFBundleIdentifier"] == "local.jarvis.ax-fixture"
    assert plist["CFBundleName"] == plist["CFBundleDisplayName"] == "JARVIS Fixture"
    assert plist["CFBundleExecutable"] == "Python" and plist["CFBundlePackageType"] == "APPL"
    assert bundle.executable == bundle.path / "Contents" / "MacOS" / "Python" and bundle.executable.read_text() == "#!mach-o"
    assert (bundle.path / "Contents" / "Resources" / "PythonInterpreter.icns").exists(), "the rest of the app comes along"
    assert bundle.source == interpreter.app


def test_the_copy_registers_as_a_handler_for_nothing(fb, interpreter):
    plist = info(make(fb, interpreter))
    for key in fb.DROPPED_KEYS:
        assert key not in plist, f"{key} would make a throwaway copy a handler for something"
    assert {"CFBundleDocumentTypes", "CFBundleURLTypes", "UTExportedTypeDeclarations", "NSAppleScriptEnabled"} <= set(
        fb.DROPPED_KEYS), "the ones that matter for .py files in particular"
    assert plist["LSMinimumSystemVersion"] == "11.0" and plist["NSHighResolutionCapable"] is True, "the rest is kept"


def test_the_environment_carries_the_site_packages_pyobjc_is_in(fb, interpreter):
    plist = info(make(fb, interpreter, packages=["/venv/lib/python3.14/site-packages", "/extra/site-packages"]))
    assert plist["LSEnvironment"] == {"PYTHONPATH": "/venv/lib/python3.14/site-packages:/extra/site-packages",
                                      "PYTHONDONTWRITEBYTECODE": "1"}


def test_the_fixture_is_inside_the_bundle_and_the_original_is_left_alone(fb, interpreter):
    before = (interpreter.app / "Contents" / "Info.plist").read_bytes()
    bundle = make(fb, interpreter)
    assert bundle.script == bundle.path / "Contents" / "Resources" / "ax_fixture.py"
    assert bundle.script.read_text() == "print('fixture')\n"
    assert (interpreter.app / "Contents" / "Info.plist").read_bytes() == before
    assert (interpreter.app / "Contents" / "_CodeSignature" / "CodeResources").exists()
    assert not (bundle.path / "Contents" / "_CodeSignature").exists(), "the old seal no longer matches the plist"


def test_building_again_replaces_the_bundle(fb, interpreter):
    first = make(fb, interpreter)
    (first.path / "Contents" / "stale").write_text("left over")
    second = make(fb, interpreter)
    assert second.path == first.path and not (second.path / "Contents" / "stale").exists()


def test_it_is_signed_ad_hoc_when_codesign_will_and_says_why_when_it_will_not(fb, interpreter):
    signer = Codesign()
    bundle = make(fb, interpreter, run=signer)
    assert bundle.signing == "ad hoc"
    assert signer.calls == [["codesign", "--force", "--deep", "--sign", "-", str(bundle.path)]]
    assert make(fb, interpreter, run=Codesign(returncode=1, stderr="no identity")).signing == (
        "not signed (codesign exit 1: no identity)")
    assert make(fb, interpreter, run=Codesign(error=FileNotFoundError("codesign"))).signing.startswith(
        "not signed (FileNotFoundError")
    assert make(fb, interpreter, run=Codesign(error=subprocess.TimeoutExpired("codesign", 60))).signing.startswith(
        "not signed (TimeoutExpired")
    silent = Codesign()
    assert make(fb, interpreter, sign=False, run=silent).signing == "not signed (not asked)" and silent.calls == []


def test_an_interpreter_without_an_application_bundle_is_refused_and_says_what_to_use(fb, tmp_path):
    with pytest.raises(fb.BundleError, match=r"no Resources/Python\.app.*not a macOS framework build.*Homebrew"):
        fb.find_python_app(tmp_path)


def test_a_bundle_that_cannot_be_written_is_a_bundle_error_not_a_traceback(fb, interpreter):
    interpreter.out.mkdir()
    with pytest.raises(fb.BundleError, match="could not build"):
        fb.build(interpreter.out, interpreter.out / "no-such-fixture.py", prefix=interpreter.prefix, run=Codesign())


def test_an_application_with_no_executable_is_refused(fb, interpreter):
    (interpreter.app / "Contents" / "MacOS" / "Python").unlink()
    with pytest.raises(fb.BundleError, match="has no executable"):
        make(fb, interpreter)


def test_site_packages_are_the_existing_directories_with_that_name_and_nothing_else(fb, tmp_path):
    good = tmp_path / "venv" / "lib" / "site-packages"
    good.mkdir(parents=True)
    other = tmp_path / "lib"
    other.mkdir()
    found = fb.site_packages(["", str(other), str(good), str(good), str(tmp_path / "gone" / "site-packages")])
    assert found == [str(good)]
