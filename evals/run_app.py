"""Build the Mac app and check the result — the packaged-app gate.

    PYTHONPATH=backend python -m evals.run_app

On macOS with Swift installed: runs ``macapp/build.sh``, then checks the
bundle is there, its Info.plist is valid and its signature verifies. Writes
``evals/results/app-*.json`` for the gate report. Elsewhere it says so and
writes nothing. Running the app — the window, ⌥Space, notifications — is
checked by opening it (see macapp/README.md).
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time

from .suites import ROOT

MACAPP = ROOT.parent / "macapp"
BUNDLE = MACAPP / "build" / "JARVIS.app"


def _run(*command: str) -> tuple[bool, str]:
    done = subprocess.run(command, cwd=MACAPP, capture_output=True, text=True, timeout=900)
    return done.returncode == 0, (done.stdout + done.stderr).strip()[-400:]


def main() -> int:
    if platform.system() != "Darwin" or shutil.which("swift") is None:
        print("The Mac app is built on macOS with Xcode's tools — skipped here.")
        return 0
    checks = []
    built, output = _run("./build.sh")
    checks.append({"id": "builds", "ok": built, "detail": "" if built else output})
    executable = BUNDLE / "Contents" / "MacOS" / "JARVIS"
    checks.append({"id": "bundle has its executable", "ok": executable.is_file()})
    plist_ok, plist_out = _run("plutil", "-lint", str(BUNDLE / "Contents" / "Info.plist"))
    checks.append({"id": "Info.plist is valid", "ok": plist_ok, "detail": "" if plist_ok else plist_out})
    signed, sign_out = _run("codesign", "--verify", "--verbose", str(BUNDLE))
    checks.append({"id": "signature verifies", "ok": signed, "detail": "" if signed else sign_out})
    for check in checks:
        print(f"{'PASS' if check['ok'] else 'FAIL'}  {check['id']}  {check.get('detail', '')}")
    passed = sum(c["ok"] for c in checks)
    out = ROOT / "results" / f"app-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"suite": "app", "model": "real", "summary": {
        "total": len(checks), "passed": passed, "success_rate": round(passed / len(checks), 3)},
        "results": checks}, indent=2), encoding="utf-8")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
