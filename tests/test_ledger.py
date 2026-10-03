"""The version the app shows in its top left: the number in the latest commit's title.

It lives in one file (backend/jarvis/ledger.py), is set in every commit by scripts/ledger.py, is enforced by
a commit-msg hook, and is checked here so that it can never lag the commits it is meant to name."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jarvis import ledger as ledger_module

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ledger.py"
HOOK = ROOT / "scripts" / "git-hooks" / "commit-msg"


@pytest.fixture
def lg(tmp_path, monkeypatch):
    """scripts/ledger.py as a module, pointed at a scratch copy of the ledger file."""
    spec = importlib.util.spec_from_file_location("ledger_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scratch = tmp_path / "ledger.py"
    shutil.copy(ROOT / "backend" / "jarvis" / "ledger.py", scratch)
    monkeypatch.setattr(module, "LEDGER_FILE", scratch)
    return module


def test_the_ledger_version_is_in_the_form_of_a_commit_title():
    assert re.fullmatch(r"v\d+\.\d+", ledger_module.LEDGER_VERSION)


def test_the_script_reads_the_value_the_app_reports():
    spec = importlib.util.spec_from_file_location("ledger_script_real", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.read() == ledger_module.LEDGER_VERSION


def test_the_ledger_never_lags_the_latest_commits_title(lg):
    """The commit that carries a version carries it in this file too: a checkout whose latest commit is titled
    v8.60 cannot say v8.59. (Ahead is fine - that is the commit being made. No git, or a latest commit that is
    not titled with a version, and there is nothing to compare.)"""
    title = lg.latest_title()
    if not title:
        pytest.skip("no ledger-titled commit to compare with")
    assert not lg.behind(ledger_module.LEDGER_VERSION, title), (
        f"backend/jarvis/ledger.py says {ledger_module.LEDGER_VERSION}, the latest commit is titled {title}: "
        f"python scripts/ledger.py set {title}")


def test_versions_are_parsed_ordered_and_bumped_like_the_ledger_counts(lg):
    assert lg.parse("v8.57") == (8, 57) and lg.parse("v10.0") == (10, 0)
    assert lg.bump("v8.57") == "v8.58" and lg.bump("v8.9") == "v8.10", "minor: no ceiling, numerically"
    assert lg.bump("v8.57", major=True) == "v9.0", "a major resets the minor"
    assert lg.behind("v8.9", "v8.10") and lg.behind("v8.57", "v9.0")
    assert not lg.behind("v8.10", "v8.10") and not lg.behind("v9.0", "v8.57") and not lg.behind("v8.1", "")
    for bad in ("8.57", "v8", "v8.57.1", "V8.57", "v8.x", ""):
        with pytest.raises(ValueError):
            lg.parse(bad)


def test_setting_the_version_rewrites_only_that_line_and_reads_back(lg):
    before = lg.LEDGER_FILE.read_text()
    lg.write("v12.3")
    after = lg.LEDGER_FILE.read_text()
    assert lg.read() == "v12.3"
    assert [a for a, b in zip(before.splitlines(), after.splitlines()) if a != b] == [
        f'LEDGER_VERSION = "{ledger_module.LEDGER_VERSION}"']
    with pytest.raises(ValueError):
        lg.write("8.58")
    assert lg.read() == "v12.3", "a bad value changes nothing"
    with pytest.raises(ValueError):
        lg.read("no such line")


def test_the_command_line(lg, capsys):
    current = ledger_module.LEDGER_VERSION
    assert lg.main(["show"]) == 0 and capsys.readouterr().out.strip() == current
    assert lg.main(["next"]) == 0 and capsys.readouterr().out.strip() == lg.bump(current)
    assert lg.main(["next", "--major"]) == 0 and capsys.readouterr().out.strip() == lg.bump(current, major=True)
    assert lg.main(["set", "v99.1"]) == 0 and lg.read() == "v99.1"
    assert lg.main(["set", "oops"]) == 2 and lg.main(["set"]) == 2
    assert lg.main(["frobnicate"]) == 2


@pytest.mark.parametrize("message, staged, fragment", [
    ("v8.59\n\nbody", "v8.59", ""),
    ("v8.59\n", "v8.58", "titled v8.59 but the app would say v8.58"),
    ("v8.59\n", None, "is not staged"),
    ("Merge branch 'x'\n", "v8.58", ""),
    ("Initial commit\n", None, ""),
    ("", "v8.58", ""),
    ("v8.59 and some more words\n", "v8.58", ""),
])
def test_a_commit_message_must_carry_the_staged_version(lg, tmp_path, message, staged, fragment):
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message)
    problem = lg.message_problem(str(path), staged)
    assert (fragment in problem) if fragment else problem == ""


def _repo(tmp_path: Path, ledger_text: str) -> Path:
    """A scratch repository with the script, the hook and a ledger file, hooks enabled."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "git-hooks").mkdir(parents=True)
    (repo / "backend" / "jarvis").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "ledger.py")
    shutil.copy(HOOK, repo / "scripts" / "git-hooks" / "commit-msg")
    (repo / "backend" / "jarvis" / "ledger.py").write_text(ledger_text)
    for command in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@example.com"],
                    ["config", "core.hooksPath", "scripts/git-hooks"]):
        subprocess.run(["git", "-C", str(repo), *command], check=True)
    return repo


def _commit(repo: Path, title: str) -> subprocess.CompletedProcess:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", title], capture_output=True, text=True)


def test_the_hook_refuses_a_commit_whose_title_the_app_would_not_show(tmp_path):
    repo = _repo(tmp_path, 'LEDGER_VERSION = "v8.58"\n')
    refused = _commit(repo, "v8.59")
    assert refused.returncode != 0 and "titled v8.59 but the app would say v8.58" in refused.stderr
    assert subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).returncode != 0, \
        "nothing was committed"


def test_the_hook_lets_a_commit_through_once_the_ledger_says_the_same_and_ignores_other_titles(tmp_path):
    repo = _repo(tmp_path, 'LEDGER_VERSION = "v8.58"\n')
    assert _commit(repo, "v8.58").returncode == 0
    (repo / "backend" / "jarvis" / "ledger.py").write_text('LEDGER_VERSION = "v8.59"\n')
    assert _commit(repo, "v8.59").returncode == 0
    (repo / "notes.txt").write_text("x")
    assert _commit(repo, "tidy notes").returncode == 0, "a title that is not a version is not the ledger's business"
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.splitlines() == ["tidy notes", "v8.59", "v8.58"]


def test_the_check_command_catches_a_ledger_left_behind_by_a_commit(tmp_path):
    repo = _repo(tmp_path, 'LEDGER_VERSION = "v8.58"\n')
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "/nonexistent"], check=True)   # no hook: a slip
    assert _commit(repo, "v8.59").returncode == 0
    behind = subprocess.run(["python3", str(repo / "scripts" / "ledger.py"), "check"], capture_output=True, text=True)
    assert behind.returncode == 1 and "says v8.58 but the latest commit is titled v8.59" in behind.stdout
    (repo / "backend" / "jarvis" / "ledger.py").write_text('LEDGER_VERSION = "v8.59"\n')
    ahead = subprocess.run(["python3", str(repo / "scripts" / "ledger.py"), "check"], capture_output=True, text=True)
    assert ahead.returncode == 0 and "ok: v8.59" in ahead.stdout


def test_the_hook_is_executable_and_calls_the_script():
    assert HOOK.stat().st_mode & 0o111, "git runs it directly"
    assert "ledger.py" in HOOK.read_text() and "check-message" in HOOK.read_text()


# -- what the interface does with it ---------------------------------------------------------------
def test_the_interface_is_built_from_the_same_file_and_shows_the_ledger_not_the_hash():
    vite = (ROOT / "frontend" / "vite.config.ts").read_text()
    assert "backend/jarvis/ledger.py" in vite and "__UI_LEDGER__" in vite and "LEDGER_VERSION" in vite
    bar = (ROOT / "frontend" / "src" / "components" / "StatusBar.tsx").read_text()
    assert "versionLabel(status, __UI_LEDGER__)" in bar and "version.main" in bar
    assert "status.commit" not in bar, "the hash is for the tooltip (lib/version.ts), not the label"
    label = (ROOT / "frontend" / "src" / "lib" / "version.ts").read_text()
    assert "const main = ledger ||" in label, "the ledger version leads; an older backend falls back to the package's"
    assert "status.commit" in label, "the commit hash is still there, in the tooltip"


def test_the_rule_is_written_down_where_the_next_commit_will_read_it():
    rules = (ROOT / "CLAUDE.md").read_text()
    assert "backend/jarvis/ledger.py" in rules and "scripts/ledger.py set" in rules
    assert "core.hooksPath" in rules and "Every commit sets it" in rules
