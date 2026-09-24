#!/usr/bin/env bash
# Release JARVIS v3.0 — only if every release gate passes on this Mac.
#
# Runs every benchmark with your own models (scripts/bench_all.sh), reads the
# gate report, and only if everything passes: sets the version to 3.0.0,
# commits, and tags v3.0 locally. It never pushes; it tells you how.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "You have uncommitted changes — commit or stash them first." >&2
  exit 1
fi
if git rev-parse -q --verify refs/tags/v3.0 >/dev/null; then
  echo "v3.0 is already tagged." >&2
  exit 1
fi

./scripts/bench_all.sh "$@"

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
if ! PYTHONPATH=backend $PY -m evals.report >/dev/null; then
  echo
  echo "Not every gate passes — the report above says which. Nothing was tagged."
  exit 1
fi

$PY - <<'PYTHON'
import json
import pathlib
import re

def swap(path, pattern, replacement):
    file = pathlib.Path(path)
    text = file.read_text(encoding="utf-8")
    new = re.sub(pattern, replacement, text, count=1, flags=re.M)
    if new == text:
        raise SystemExit(f"couldn't set the version in {path}")
    file.write_text(new, encoding="utf-8")

swap("backend/jarvis/__init__.py", r'^__version__ = ".*"$', '__version__ = "3.0.0"')
swap("pyproject.toml", r'^version = ".*"$', 'version = "3.0.0"')
swap("macapp/Resources/Info.plist", r"(<key>CFBundleVersion</key>\s*<string>)[^<]*", r"\g<1>3.0.0")
for path in ("frontend/package.json", "frontend/package-lock.json"):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    data["version"] = "3.0.0"
    if "packages" in data and "" in data["packages"]:
        data["packages"][""]["version"] = "3.0.0"
    pathlib.Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYTHON

git commit -qam "JARVIS v3.0"
git tag -a v3.0 -m "JARVIS v3.0 — every release gate passed on $(date +%Y-%m-%d)"
echo
echo "Every gate passed: committed and tagged v3.0."
echo "Push it with:  git push origin main v3.0"
