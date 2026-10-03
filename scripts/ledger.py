#!/usr/bin/env python3
"""The ledger version the app shows in its top left: read it, bump it, check it against the commits.

    python scripts/ledger.py show              # v8.58
    python scripts/ledger.py next              # v8.59   (v9.0 with --major)
    python scripts/ledger.py set v8.59         # write it to backend/jarvis/ledger.py
    python scripts/ledger.py check             # fail if it is behind the latest commit's title
    python scripts/ledger.py check-message F   # commit-msg hook: the title must equal the staged value

Every commit sets it to its own title, in the same commit (CLAUDE.md, "The version in the app").
Pure standard library, so a hook can run it anywhere.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = ROOT / "backend" / "jarvis" / "ledger.py"
RELATIVE = "backend/jarvis/ledger.py"
PATTERN = re.compile(r'^LEDGER_VERSION = "(v\d+\.\d+)"$', re.MULTILINE)
TITLE = re.compile(r"^v\d+\.\d+$")


def parse(version: str) -> tuple[int, int]:
    """``"v8.57"`` -> ``(8, 57)``."""
    if not TITLE.match(version):
        raise ValueError(f"not a ledger version: {version!r} (expected v<major>.<minor>, like v8.57)")
    major, minor = version[1:].split(".")
    return int(major), int(minor)


def bump(version: str, *, major: bool = False) -> str:
    """The next ledger version: v8.57 -> v8.58, or v9.0 for a major (new outside software, a new form)."""
    current_major, current_minor = parse(version)
    return f"v{current_major + 1}.0" if major else f"v{current_major}.{current_minor + 1}"


def read(text: str | None = None) -> str:
    text = LEDGER_FILE.read_text() if text is None else text
    found = PATTERN.search(text)
    if not found:
        raise ValueError(f'no LEDGER_VERSION = "v<major>.<minor>" line in {RELATIVE}')
    return found.group(1)


def write(version: str) -> None:
    parse(version)
    LEDGER_FILE.write_text(PATTERN.sub(f'LEDGER_VERSION = "{version}"', LEDGER_FILE.read_text(), count=1))


def git(*args: str, root: Path = ROOT) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    return done.stdout.strip() if done.returncode == 0 else ""


def latest_title(root: Path = ROOT) -> str:
    """The latest commit's title if it is a ledger version, else ''."""
    title = git("log", "-1", "--format=%s", root=root)
    return title if TITLE.match(title) else ""


def behind(version: str, title: str) -> bool:
    """Whether *version* is older than the commit title *title* (one that carries no version is never ahead)."""
    return bool(title) and parse(version) < parse(title)


def message_problem(message_file: str, staged: str | None) -> str:
    """Why a commit message's title cannot go with the staged ledger value, or ''. A title that is not a
    ledger version (a merge, the initial commit) is none of this check's business."""
    lines = Path(message_file).read_text().splitlines()
    title = lines[0].strip() if lines else ""
    if not TITLE.match(title):
        return ""
    if staged is None:
        return f"{RELATIVE} is not staged: set it to {title} (python scripts/ledger.py set {title}) and git add it."
    if staged != title:
        return (f"the commit is titled {title} but the app would say {staged}: "
                f"python scripts/ledger.py set {title}, git add {RELATIVE}, and commit again.")
    return ""


def main(argv: list[str]) -> int:
    command, rest = (argv[0] if argv else "show"), argv[1:]
    try:
        if command == "show":
            print(read())
        elif command == "next":
            print(bump(read(), major="--major" in rest))
        elif command == "set":
            write(rest[0])
            print(read())
        elif command == "check":
            version, title = read(), latest_title()
            if behind(version, title):
                print(f"{RELATIVE} says {version} but the latest commit is titled {title}: "
                      f"the app's version label is behind. python scripts/ledger.py set {title} (or the next one).")
                return 1
            print(f"ok: {version}" + (f" (latest commit {title})" if title else ""))
        elif command == "check-message":
            staged_text = git("show", f":{RELATIVE}")
            problem = message_problem(rest[0], read(staged_text) if staged_text else None)
            if problem:
                print(f"ledger: {problem}", file=sys.stderr)
                return 1
        else:
            print(__doc__)
            return 2
    except (ValueError, IndexError, OSError) as exc:
        print(f"ledger: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
