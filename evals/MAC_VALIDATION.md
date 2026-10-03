# Real-Mac validation of native control (Phase 1C)

JARVIS's native control — Accessibility reads and actions, genuine key and mouse
input, menus, OCR marks, re-finding stale handles, the optional observer thread —
is covered by over a thousand tests, **none of which has touched a real Mac**. CI
is Linux and runs it all against a fake accessibility tree. This is the run that
changes that, and the baseline every later question about the native layer
(is another backend faster? is the observer worth turning on?) is measured against.

Nothing here changes a setting. Everything it opens, it closes: Calculator, a blank
TextEdit document (cancelled and closed, never saved), a throwaway folder on the
Desktop, and a small window of its own (`scripts/ax_fixture.py`).

## Before

* On the Mac, in the repo: `git fetch origin claude/main-work && git checkout claude/main-work && git pull`
* `source .venv/bin/activate && pip install -e '.[native]'`
* Give the terminal you run this from **Accessibility** and **Screen Recording**
  (System Settings → Privacy & Security). Close anything you don't want read.

## The unit suite on the Mac

`pip install -e '.[dev,native,evals]'`, then `python -m pytest -q`. The tests treat every
host as a non-Mac (conftest `_hermetic_host`), so running them never opens an app, clicks
or posts input on your machine. Tests that skip are the live-browser ones, which need
Playwright and its bundled Chromium (`pip install -e '.[browser]' && playwright install chromium`);
that is a missing optional dependency, not a failure. Anything that fails here is a real
finding: send `python -m pytest -q --tb=short` for just those tests.

## The order

```
python scripts/check_native.py                 # 1  look: read a window, menus, OCR
python scripts/check_native.py --act           # 2  a TextEdit round trip
python scripts/check_native.py --controls      # 3  click, pop-up, drag, marks
python scripts/check_native.py --stale         # 4  re-finding a rebuilt control, refusing look-alikes
python scripts/check_native.py --observe       # 5  do AXObserver notifications fire, and are they worth it?
PYTHONPATH=backend python -m evals.run_mac --tasks textedit-save-plain-text,finder-drag-into-folder,settings-dock-autohide   # 6
python scripts/check_native.py --all --repeat 5   # 7  everything, five times, with timings and the criteria table
```

Every run starts with a line like `code: v8.53 (3f2a9c1)`. Send it with the output: it is how a
result is matched to the code that produced it (`git pull` first if it is older than expected;
`+ uncommitted changes` means the checkout has been edited).

Run 1 first. If it fails, stop and send it back: everything after rests on it. After
1–5 each pass once, run 7 for repeatability and timings. Only then think about
turning anything on.

## What the marks mean

* ✓ passed · ✗ failed · ⚠ **couldn't tell** (for example macOS kept the old reference
  valid, so there was nothing to re-find). A ⚠ is not a pass.
* A ✗ prints the labels the window actually showed. That is the evidence a fix is
  made from — paste it back whole.
* The one that matters most is `PRESSED …` under `--stale`: it means a control was
  pressed that JARVIS could not be sure was the one asked for. "Couldn't find Save"
  is an inconvenience; "found the wrong Save and pressed it" is not.

## The exit criteria

`--all` ends with this table, and `--json` records it. Phase 1C is done when no row
is ✗ or ⚠ and the two manual rows have been done by hand.

| Area | Requirement | How it is shown |
|---|---|---|
| AX observation | real applications successfully inspected | `look` |
| AX actions | press and set-value actually work | `--controls`, `--act` |
| Keyboard | real input to the target process verified | `--act`, `--controls` |
| Windows | background targeting verified | `--stale` (press with another app in front) |
| Menus | real menu traversal verified | `--act`, `--controls` |
| Stale handles | stale → re-found → verified; ambiguity and look-alikes refused | `--stale` |
| Controls | Calculator, TextEdit and Finder checks pass | `--controls` |
| Save sheet | the real TextEdit save flow works | `--act`, `--controls` |
| Observer | real notifications received, none missed, none raising, no CPU or thread left over | `--observe` |
| Observer fallback | polling still works without it | `--observe` |
| Permissions | the right failure when a permission is off | **by hand**, below |
| Evals | the three new Mac tasks pass | **by hand**, step 6 |
| Repeatability | every critical check passes every time it is run | `--repeat 3` or more |
| Performance | baseline timings captured | any run, in the JSON |

**Permissions, by hand.** Turn Accessibility off for the terminal and run
`python scripts/check_native.py`: it should print the "Allow it in System Settings"
line and exit 1 — not a traceback. Turn it back on, turn Screen Recording off, run it
again: the screenshot step should say `screencapture failed — is Screen Recording
allowed?` and the rest should still run.

## Timings

For every check the run records seconds in each phase — **observation** (reading a
window), **locator** (finding the element behind a handle, including re-finding),
**action** (pressing, typing, choosing, dragging), **refresh** (looking again after
acting), **verification** (confirming by something other than the accessibility tree:
the clipboard, the file system, the fixture's own log), **settle** (waits the check
inserts for an app to catch up — kept apart so they don't pass for JARVIS being slow)
and **total**. With `--repeat N` the table shows the median and, in brackets, the worst.
This is the baseline: any other way of driving the Mac is compared against these
numbers, not against a guess.

## The observer, and when to turn it on

`automation.native_observer` is off. It is a latency optimisation (it ends a wait as
soon as the app says the thing happened, instead of at the next 50 ms poll), not a
reliability feature — the poll underneath decides and is unchanged either way. Turn
it on only when **both** hold:

1. **Correct**: `--observe` passes on repeated runs (use `--all --repeat 5`) — every
   notification arrives, none missed, no callback raising, no CPU or thread left over,
   the event loop not stalled, and waits still work with it stopped.
2. **Useful**: its `usefulness` line shows a saving worth having. A median of a few
   milliseconds is "no measurable gain — leave it off"; tens of milliseconds is a
   small latency optimisation; only that second case justifies the extra thread.

## Sending it back

Send the terminal output of each step and the `native-validation-*.json` that `--all`
writes. Both include the labels visible in the windows the checks open — Calculator,
a TextEdit save sheet, a Finder window (whose sidebar can show your name and locations)
— so skim them first.
