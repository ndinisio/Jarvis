"""Run the native macOS suite — on the Mac, with your real configuration.

    PYTHONPATH=backend python -m evals.run_mac
    PYTHONPATH=backend python -m evals.run_mac --tasks notes-create,volume-set

Each task speaks to a real JARVIS (real models, real macOS automation) and is
checked by a shell command afterwards. Work happens inside
~/Desktop/JARVIS-bench, which is created fresh and removed at the end; items
created in Notes, Reminders, Calendar and Mail are marked "JARVIS-bench" and
deleted by each task's cleanup. JARVIS needs its usual macOS permissions
(Accessibility, Automation) — grant them to the terminal you run this from.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jarvis.core.config import ConfigStore, load_config

from .harness import TaskResult, apply_overrides, drive_turn
from .run_web import parse_overrides
from .suites import ROOT, load_mac_tasks

BENCH = Path.home() / "Desktop" / "JARVIS-bench"


def shell(command: str, timeout: float = 30.0) -> tuple[bool, str]:
    if not command.strip():
        return True, ""
    env = {**os.environ, "BENCH": str(BENCH)}
    try:
        done = subprocess.run(["/bin/bash", "-c", command], env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return done.returncode == 0, (done.stdout + done.stderr).strip()[-400:]


async def run(args) -> list[TaskResult]:
    from jarvis.core.app import JarvisApp

    wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
    tasks = [t for t in load_mac_tasks() if not wanted or t.id in wanted]
    results = []
    overrides = parse_overrides(args.set)
    with tempfile.TemporaryDirectory(prefix="jarvis-mac-eval-") as tmp:
        for task in tasks:
            shutil.rmtree(BENCH, ignore_errors=True)
            BENCH.mkdir(parents=True)
            ok, output = shell(task.setup)
            record = TaskResult(id=task.id, category="mac", phrasing=task.utterance, ok=False)
            if not ok:
                record.failures = [f"setup failed: {output}"]
                results.append(record)
                continue
            config = apply_overrides(load_config(Path(args.config) if args.config else None), overrides)
            config.workspace = str(Path(tmp) / task.id)
            config.log_level = "WARNING"
            config.voice.enabled = False
            config.voice.tts_engine = "off"
            config.ensure_workspace()
            app = JarvisApp(ConfigStore(config, Path(tmp) / task.id / "config.json"), enable_voice=False)
            await drive_turn(app, task.utterance, record, approve=task.approve, timeout_s=args.timeout)
            await asyncio.sleep(1.0)  # let the app finish what it was asked to do
            passed, output = shell(task.check)
            record.failures = [] if passed else [f"check failed: {output or 'exit status non-zero'}"]
            if record.error:
                record.failures.insert(0, record.error)
            record.ok = not record.failures
            shell(task.cleanup)
            await app.shutdown()
            results.append(record)
            mark = "PASS" if record.ok else "FAIL"
            print(f"{mark}  {task.id:<22} {record.wall_s:6.1f}s  tools={record.tool_calls:<3} "
                  f"{task.utterance[:60]}", flush=True)
            if not record.ok:
                for failure in record.failures:
                    print(f"      - {failure}")
    shutil.rmtree(BENCH, ignore_errors=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    if platform.system() != "Darwin":
        print("The native suite drives real macOS apps — run it on the Mac.")
        return 2
    started = time.time()
    results = asyncio.run(run(args))
    passed = sum(r.ok for r in results)
    summary = {"total": len(results), "passed": passed,
               "success_rate": round(passed / len(results), 3) if results else 0.0}
    print(json.dumps(summary, indent=2))
    out = Path(args.out) if args.out else ROOT / "results" / f"mac-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"suite": "mac", "model": "real", "label": args.label, "started": started, "summary": summary,
                               "results": [r.as_dict() for r in results]}, indent=2), encoding="utf-8")
    print(f"results written to {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
