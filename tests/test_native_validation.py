"""scripts/native_validation.py: recording, timing and reporting for a real-Mac pass.

It knows nothing about macOS, so all of it is tested for real here.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def nv():
    spec = importlib.util.spec_from_file_location("native_validation_script", SCRIPTS / "native_validation.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                    # dataclasses looks its module up here
    spec.loader.exec_module(module)
    return module


class FakeSurface:
    """A surface whose calls take a set time, counted as resolve time too."""

    def __init__(self):
        self.resolve_seconds = 0.0
        self.relocations = 3
        self.plain = "untouched"

    async def read(self, app=""):
        await asyncio.sleep(0.03)
        return "snapshot"

    async def press(self, handle):
        await asyncio.sleep(0.05)
        self.resolve_seconds += 0.02                   # 20 ms of the 50 went on finding the element
        return "pressed"

    async def describe(self, handle):
        await asyncio.sleep(0.01)
        return {}

    async def find(self, label):
        await asyncio.sleep(0.01)
        return []

    def not_async(self):
        return "plain"


async def test_calls_are_timed_by_phase_and_locating_is_taken_out_of_acting(nv):
    recorder = nv.Recorder()
    with recorder.measure("a check", FakeSurface()) as surface:
        await surface.read("x")                        # before acting: observation
        await surface.find("x")
        await surface.press("ax1")
        await surface.read("x")                        # after acting: a refresh
        await surface.describe("ax1")
    seconds = recorder.runs[0]["timings"]["seconds"]
    assert seconds["observation"] == pytest.approx(0.03, abs=0.02)
    assert seconds["refresh"] == pytest.approx(0.03, abs=0.02)
    assert seconds["locator"] == pytest.approx(0.01 + 0.02, abs=0.02)
    assert seconds["action"] == pytest.approx(0.03, abs=0.02), "the 50 ms press, less the 20 ms spent finding"
    assert seconds["verification"] == pytest.approx(0.01, abs=0.02)
    assert recorder.runs[0]["timings"]["total"] >= 0.12


async def test_other_attributes_and_plain_methods_pass_straight_through(nv):
    recorder = nv.Recorder()
    surface = FakeSurface()
    with recorder.measure("x", surface) as timed:
        assert timed.plain == "untouched" and timed.relocations == 3
        assert timed.not_async() == "plain"


async def test_a_run_passes_when_every_step_it_recorded_passed(nv):
    recorder = nv.Recorder()
    with recorder.measure("good", FakeSurface()):
        recorder.record("one", "pass")
        recorder.record("two", "pass")
    with recorder.measure("bad", FakeSurface()):
        recorder.record("one", "pass")
        recorder.record("two", "inconclusive")
    with recorder.measure("silent", FakeSurface()):
        pass
    assert [run["passed"] for run in recorder.runs] == [True, False, None]
    recorder.mark("silent", True)
    assert recorder.runs[2]["passed"] is True


async def test_a_check_that_raises_is_recorded_as_failed_and_the_error_goes_on(nv):
    recorder = nv.Recorder()
    with pytest.raises(RuntimeError), recorder.measure("boom", FakeSurface()):
        recorder.record("fine so far", "pass")
        raise RuntimeError("x")
    assert recorder.runs[0]["passed"] is False
    assert recorder.current is None, "timing stops being attributed to the check that is over"


async def test_steps_know_which_check_and_which_run_they_belong_to(nv):
    recorder = nv.Recorder()
    recorder.iteration = 2
    with recorder.measure("outer", FakeSurface()):
        recorder.record("a", "pass", "detail")
    assert recorder.steps == [{"iteration": 2, "check": "outer", "label": "a", "status": "pass", "detail": "detail"}]


async def test_verification_and_settling_are_attributed_to_the_check_running(nv):
    recorder = nv.Recorder()
    recorder.add("settle", 5.0)                        # nothing running: dropped, not an error
    with recorder.measure("x", FakeSurface()):
        with recorder.verifying():
            await asyncio.sleep(0.02)
        recorder.add("settle", 0.5)
    seconds = recorder.runs[0]["timings"]["seconds"]
    assert seconds["verification"] >= 0.02 and seconds["settle"] == 0.5


def test_percentiles_are_nearest_rank(nv):
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert nv.percentile(values, 0.5) == 60 or nv.percentile(values, 0.5) == 50
    assert nv.percentile(values, 0.95) == 100
    assert nv.percentile([7], 0.95) == 7
    assert nv.percentile([], 0.5) == 0.0


def _run(check, iteration, passed, **seconds):
    base = dict.fromkeys(("observation", "locator", "action", "refresh", "verification", "settle"), 0.0)
    base.update(seconds)
    return {"check": check, "iteration": iteration, "passed": passed,
            "timings": {"seconds": base, "calls": {}, "total": sum(base.values()) + 0.1}}


def test_timings_are_summarised_per_check_in_milliseconds(nv):
    runs = [_run("a", 1, True, action=0.10), _run("a", 2, True, action=0.30), _run("a", 3, False, action=0.20),
            _run("b", 1, True, observation=0.05)]
    summary = nv.summarise(runs)
    assert list(summary) == ["a", "b"]
    assert summary["a"]["runs"] == 3 and summary["a"]["passed"] == 2
    assert summary["a"]["phases"]["action"]["p50"] == pytest.approx(200)
    assert summary["a"]["phases"]["action"]["max"] == pytest.approx(300)
    assert summary["b"]["phases"]["observation"]["p50"] == pytest.approx(50)
    table = "\n".join(nv.format_timings(summary, 3))
    assert "median of 3 run(s), worst in brackets" in table and "2/3" in table and "1/1" in table
    assert "[300]" in table
    assert "worst in brackets" not in "\n".join(nv.format_timings(summary, 1))
    assert nv.format_timings({}, 1) == []


def _steps(**labels):
    """{"label": "pass"|"fail"|... or a list of statuses per run}."""
    out = []
    for label, statuses in labels.items():
        for number, status in enumerate(statuses if isinstance(statuses, list) else [statuses], 1):
            out.append({"iteration": number, "check": "c", "label": label.replace("_", " "), "status": status,
                        "detail": ""})
    return out


def test_a_criterion_is_shown_only_when_every_label_under_it_passed(nv):
    labels = dict.fromkeys(("read the window", "menus", "text on a screenshot"), "pass")
    rows = {row[0]: row for row in nv.evaluate(_steps(**{k.replace(" ", "_"): v for k, v in labels.items()}), [], 1)}
    assert rows["AX observation"][2] == "pass"
    broken = {**labels, "menus": "fail"}
    rows = {row[0]: row for row in nv.evaluate(_steps(**{k.replace(" ", "_"): v for k, v in broken.items()}), [], 1)}
    assert rows["AX observation"][2] == "fail" and "menus (fail)" in rows["AX observation"][3]


def test_a_label_never_reached_is_not_run_and_an_undecided_one_is_inconclusive(nv):
    steps = _steps(read_the_window="pass", menus="pass")
    rows = {row[0]: row for row in nv.evaluate(steps, [], 1)}
    assert rows["AX observation"][2] == "not run" and "text on a screenshot (not run)" in rows["AX observation"][3]
    steps = _steps(read_the_window="pass", menus="inconclusive", text_on_a_screenshot="pass")
    assert {row[0]: row for row in nv.evaluate(steps, [], 1)}["AX observation"][2] == "inconclusive"
    steps = _steps(read_the_window="fail", menus="inconclusive")
    assert {row[0]: row for row in nv.evaluate(steps, [], 1)}["AX observation"][2] == "fail", "a failure outranks the rest"


def test_manual_rows_are_manual_and_performance_needs_timings(nv):
    rows = {row[0]: row for row in nv.evaluate([], [], 1)}
    assert rows["Permissions"][2] == "manual" and rows["Evals"][2] == "manual"
    assert rows["Performance"][2] == "not run"
    rows = {row[0]: row for row in nv.evaluate([], [_run("a", 1, True, action=0.1)], 1)}
    assert rows["Performance"][2] == "pass" and "1 check(s) timed" in rows["Performance"][3]


def test_repeatability_needs_enough_runs_and_every_critical_check_passing_in_all_of_them(nv):
    rows = {row[0]: row for row in nv.evaluate([], [], 1)}
    assert rows["Repeatability"][2] == "not run" and "--repeat 3" in rows["Repeatability"][3]
    steady = {label: ["pass"] * 3 for label in nv.CRITICAL}
    assert nv.evaluate(_flat(steady), [], 3)[-2][2] == "pass"
    flaky = {**steady, "menus": ["pass", "fail", "pass"]}
    row = nv.evaluate(_flat(flaky), [], 3)[-2]
    assert row[2] == "fail" and "menus (2/3)" in row[3]


def _flat(by_label):
    out = []
    for label, statuses in by_label.items():
        for number, status in enumerate(statuses, 1):
            out.append({"iteration": number, "check": "c", "label": label, "status": status, "detail": ""})
    return out


def test_the_criteria_table_is_readable(nv):
    text = "\n".join(nv.format_criteria(nv.evaluate([], [], 1)))
    assert text.startswith("Exit criteria for real-Mac validation")
    assert "☐ Permissions" in text and "· AX observation" in text


def test_the_report_is_json_and_says_where_it_ran(nv):
    recorder = nv.Recorder()
    with recorder.measure("a", FakeSurface()):
        recorder.record("read the window", "pass")
    report = nv.build_report(recorder, argv=["--all"], repeat=1, ok=True)
    assert json.loads(json.dumps(report))["schema"] == 1
    assert report["argv"] == ["--all"] and report["ok"] is True
    assert {"macos", "machine", "chip", "python", "platform"} <= set(report["environment"])
    assert report["timings"]["a"]["runs"] == 1
    assert any(row["area"] == "AX observation" for row in report["criteria"])


def test_every_label_a_criterion_waits_for_is_one_the_checks_actually_report(nv):
    """A criterion that names a step the script never prints could never be
    met; this ties the table to the checks so a rename can't silently break it."""
    tree = ast.parse((SCRIPTS / "check_native.py").read_text())
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    missing = [label for criterion in nv.CRITERIA for label in criterion.labels if label not in literals]
    assert not missing, f"nothing in check_native.py reports: {missing}"
