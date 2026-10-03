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

A run that ends with `Segmentation fault` (exit 139) is a bug in the harness or the backend, not a
finding about an app: rerun it as `python -X faulthandler scripts/check_native.py …` and send the
stack. (PyObjC turns `None` into a NULL pointer, and handing NULL to CoreFoundation kills the
process; the backend keeps `None` away from it - tests/test_backend_cf.py.)

Each check starts from the app you ran it in (`Starting point: Terminal …`), not from whichever
app an earlier check left in front, so any of 1–5 can be run on its own, in any state. `--app NAME`
starts somewhere else.

Run 1 first. If it fails, stop and send it back: everything after rests on it. After
1–5 each pass once, run 7 for repeatability and timings. Only then think about
turning anything on.

## Reading the background-press line (`--stale`)

`a press works with another app in front` separates what JARVIS owns from what macOS decides. The v8.58
real-Mac evidence (a bare `AXUIElementPerformAction` from another process, a button with no action, and
Calculator's own clear button all bring their app forward) means the app coming forward on an AXPress is not
something JARVIS does and not something it can prevent, so it is **reported, not demanded**. What the step
fails on is what JARVIS controls:

* the press was a semantic AXPress that the app accepted - not a coordinate click (which activates the app and
  moves the pointer), not a refused or absent AXPress that fell back to one;
* JARVIS posted no other synthetic input (move, key, typing, drag) and asked for no app to be brought forward;
* the app itself recorded **exactly one** press, of the control that was read (name, identifier and which build
  of the window) - not none, not two, not a look-alike.

Its ✗ names which one broke (`AXPress did not do the press … fell back to a coordinate click`, `AXPress succeeded
but the app recorded no press`, `the app recorded 2 presses for one`, `JARVIS also posted synthetic input (move)`,
`JARVIS also asked for an app to be brought forward (pid …)`, `the press raised …`). Each line ends with what the
surface returned (`'Pressed “Save”.'` is AXPress; `'Clicked …'` is the click) and what the window recorded.

**Nothing is pressed until the fixture itself says it is in the background.** Accessibility naming Finder as the
focused application is not that: it is a different account, and v8.59's run showed the fixture still active by its
own (`NSApplication.isActive`) while Accessibility already named another app. So before the press the harness asks
Finder to come forward (`AXFrontmost`, the way JARVIS brings an app forward) and waits - on the fixture's own answer,
not on a timer - until the fixture's `probe` has said `isActive=false` twice running with nothing in its own log
saying it became active in between. If it still says it is active, it is asked once to deactivate itself
(`NSApplication.deactivate`, public, macOS 14+) and waited for the same way. If neither does it, **the step is a
⚠ `not run: the target could not be put in the background by its own account, so nothing was pressed`** - it did
not test a background press, and no press was made, so the checks above are not exercised by that run either. If it
had gone inactive and its own log then shows it became active again before the AXPress was issued, the step is a ⚠
`baseline lost`.

Under every result there are `baseline:` lines in time order (seconds from the first request): what was asked of
Finder (accepted or refused, and when Accessibility first named it frontmost), what the fixture said about itself at
each change (`isActive`, whether its window is key, `ls_active` / `ls_front` - what LaunchServices says of it and
which pid it calls frontmost - `hidden`, `bundled` - whether the process has a bundle identifier, a bare
interpreter does not), what Accessibility said (the focused application and the fixture's own `AXFrontmost`), the
self-deactivate request, `AXPress issued` with how long after the confirming probe looked, and the fixture's own
`activation:became` / `activation:resigned` lines. They are the evidence for *why* a target stays active: the fixture
saying `isActive=true` with `ls_front` naming Finder means AppKit has not caught up; `ls_front` naming the fixture
means LaunchServices never moved; `bundled=false` points at the fixture being a bare interpreter. Send them whole.

After the `·  foreground:` is what happened to the target's standing, by the **app's own account** (AppKit's
`isActive`, asked of the fixture with the `probe` command - not Accessibility's idea of the focused application,
which a press can move without the app ever activating). With the baseline shown, the same AXPress is made by a
bare client (nothing of JARVIS in the process) as a control - itself only after the fixture has again shown it is
inactive - and the foreground is judged against it:

* ✓ `stayed inactive by its own account` - nothing came forward (`only Accessibility's focus moved to it` when
  that did - not an activation);
* ✓ `became active, as it does for the same AXPress from a bare client` - the OS's or the app's response, which
  JARVIS requested nothing of;
* ⚠ cannot be assessed (`could not say whether it is active`, or no bare-client press could be made, with the
  reason) - a ⚠ is not a pass, and says what is missing;
* ✗ `became active for JARVIS's press but not for the same AXPress from a bare client` - JARVIS's way of
  invoking the press adds an activation. This is the only foreground finding that fails the step, and it is
  followed by `evidence:` lines (the button with no action, Calculator's clear button, and the fixture's own log
  of whether it was already active when its action began and whether it ever reported becoming active) that say
  where it comes from.

Whether an app is activated when it is asked to press a button is not documented either way; this step does not
claim that it is inherent, only that JARVIS adds nothing to it. What it cannot show is that the press left a
different app, or a person's typing, undisturbed: that is not measured here.

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
