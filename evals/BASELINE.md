# Measured progress towards v3.0

Each row is a run of the suites in this folder. Oracle rows measure the
architecture (could a perfect model finish, given what JARVIS shows it?);
"real" rows come from runs on the Mac with actual models.

## Phase 0 — baseline (v2.5 + screen watcher + Kokoro + UI redesign)

**Web tasks, oracle: 4 / 40 (10%).** Even a perfect model fails almost
everything past the first step. The reason is the root cause found in the
plan: the automation loop shows the model only a one-line summary of each
tool result (`"60 elements found on …"`), never the element handles it
needs, so it cannot click "Add to Basket", fill a field or tick a box. The
four passes are two plain navigations plus two tasks that pass only because
nothing happened (a draft that must not be sent, and a declined send).

Other findings from the same run:

- "Search for a USB-C cable on Amazon and add it to my basket" is hijacked by
  the fast path into a DuckDuckGo search before the agent ever sees it.
- "remove the kettle from my amazon basket" regex-matches **`delete_file`**,
  with the path "kettle from my amazon basket". The HIGH-risk confirmation
  gate stopped it; the fast path should never have proposed it.

**Understanding corpus, fast path (deterministic): 244 / 315 (78%).**

| Group | Fast path correct |
|---|---|
| exact commands | 40/40 (100%) |
| conversational control | 11/11 (100%) |
| chat with domain words | 30/30 (100%) |
| general chat | 25/25 (100%) |
| colloquial actions | 72/77 (94%) |
| speech-recognition noise | 24/28 (86%) |
| site-qualified searches | 11/15 (73%) |
| polite single commands | 7/15 (47%) |
| **compound requests** | **21/50 (42%)** |
| **domain words that must not hijack** | **3/24 (12%)** |

The semantic (chat vs. action) stage and the native Mac suite need real
models / real macOS and are measured on the Mac (`scripts/bench_all.sh`).

## Phase 1 — root-cause fixes

**Web tasks, oracle: 38 / 40 (95%)**, up from 4 / 40. Every shopping, mail,
form and safety task now completes. The two left are the shadow-DOM widget
and the iframe, which the Phase 4 page snapshot is built to reach.

What changed:

- **The model sees the page.** Tools return a model-facing `observation`
  (a handle-addressed element listing, ranked so the page's content comes
  before its header clutter, with select options, checkbox state, open
  dialogs and a text excerpt carrying prices). After every web action the
  loop looks at the page again by itself and puts it under "What you can
  see now".
- **The page is still before JARVIS acts or looks.** A mutation-counting
  signature must stay unchanged for half a second, so a single-page app's
  re-render can't swallow what's typed next.
- **Safety judges the real element.** The permission gate inspects the
  element a click will hit (its text, id, name, link and form action)
  before asking, so "Buy Now" described as "Add to Basket" still asks, and
  a checkbox labelled "Send me the newsletter" doesn't.
- **Autonomy as chosen:** routine steps run; paying, ordering, sending,
  deleting, installing and typing into a terminal always ask.

**Understanding corpus, fast path: 315 / 315 (100%)**, up from 244 / 315.
Compound requests (50/50) and domain-word traps (24/24) no longer misroute;
polite phrasing ("could you… for me") gets the fast answer (15/15).

**All phrasings, oracle: 97.1%** — the same two Phase 4 tasks.

## Phase 3 — the interpreter

The chat-or-act decision was rewritten around how people actually talk: a
request can be asked, told, hinted or wished ("I need some AA batteries",
"pop YouTube on"), while mentioning something JARVIS could act on still
isn't asking for it. One schema-constrained call now also returns a plain
restatement of the request, which is tried against the fast path — so a
colloquial version of a simple command gets the deterministic answer — and
success criteria the operator will check its work against.

New deterministic commands for what people say most: tabs (new, close,
reopen, back, forward, reload), music (play, pause, next, previous), dark
mode, lock screen. Speech recognition prefers Whisper large-v3-turbo on the
Apple Silicon GPU and is taught the Mac's app names.

**Understanding corpus, fast path: 325 / 325.** The new colloquial group
(10 utterances like "whack on the next song") must *not* hit the fast path
directly; the real-model run scores whether the interpreter's restatement
reaches the right command (gate: ≥90%).

## Phase 4 — two browsers, genuine input, the user's part

**Web tasks, oracle: 42 / 42 (100%); every phrasing: 72 / 72.** The
shadow-DOM widget and the iframe form now complete, along with two new
tasks: a talks list behind a newsletter pop-up that swallows clicks until
it's dismissed, which only loads the wanted talk after two rounds of
scrolling; and a sign-in page where the oracle deliberately tries to type a
password (it must be refused, with the sign-in handed to the user).

What changed:

- **Which browser.** Errands that navigate run in JARVIS Chrome (its own
  profile, over the DevTools protocol); anything about the page you're on,
  and quick one-off opens, use your everyday browser. A task keeps its
  browser. Site overrides and an off switch; falls back to your browser if
  Chrome/Playwright aren't there. The evaluation harness now reaches its
  browser through this same hub (`pin`) instead of patching modules.
- **Genuine input** in JARVIS Chrome: real mouse clicks and keystrokes
  (typed character by character into fields that offer suggestions), with
  the in-page script as fallback when an element can't be reached.
- **Settling on the network, not only the DOM.** An action returns once the
  page's own requests have finished — the iframe task failed before this
  because the "Send" `fetch` was still in flight when the result was
  checked. Mutations inside same-origin frames count as page changes.
- **Deeper pages.** The listing walks open shadow roots and same-origin
  iframes (with their offsets, so "in view" stays true).
- **New page tools:** `press_page_key`, `scroll_page`, `page_go_back`,
  `wait_for_page`, `ask_user_to_take_over`. The next look says what changed
  ("New since the last look: dialog 'Added to Basket'…"), and sign-in or
  CAPTCHA pages carry an explicit instruction to hand over.
- **Safety:** password and card fields are refused by both browsers (the
  script and the genuine-input path check the real element). Pressing Enter
  in a field, or typing with `submit`, is judged by where the form goes, so
  submitting a checkout form from a postcode box still asks. A handoff is
  never pre-approved by any setting, and "done" / "I'm signed in" answers it.


## Phase 4.5 — foundations

No change to the suites' numbers (the oracle run is unchanged); this phase
closed a hole and put the checks on rails.

- **The control channel is authenticated.** Before this, any web page open on
  the Mac — including one JARVIS itself was browsing — could open JARVIS's
  WebSocket and answer a pending confirmation for you. Each run now has its own
  session token, and the event stream also refuses other sites' pages even
  with it. Proven by a test that stages exactly that attack against a pending
  "send the email" confirmation, and live in Chromium against a real server.
- **CI** runs the suite on Python 3.10 and 3.12, lint, and the interface build
  on every push and pull request. Getting it green surfaced two tests that
  silently depended on an optional package (`numpy`); they now skip cleanly
  without it and run in CI with it.
- The memory database uses write-ahead logging; a second `start.sh` says
  "JARVIS is already running" instead of crashing; `start.sh` rebuilds the
  interface when it's older than its source, so a pull can't leave an old
  interface talking to a new server.


## Phase 5 — the operator

**Web tasks, oracle: 42 / 42 (100%); every phrasing: 72 / 72** — with 26%
fewer model calls per task (5.5, from 7.4) at the same p50 wall time (5.4 s).
The saving is the milestone decomposition and the separate summary call,
both gone: a finish carries its own answer.

What changed:

- **One loop for everything** (`intelligence/operator/`). The per-turn
  decide/plan/recover loop and the errand milestone loop are replaced by one
  operator: native tool calling (emulated over text for models without it),
  several calls per reply run in order, the full result of every call in the
  conversation, and the page read again after every web action.
- **"Done" must be proven.** An errand carries a checklist — the
  interpreter's success criteria, or the goal. `finish` is refused while any
  item lacks a quote copied from something JARVIS actually saw, and the model
  is told which item and why. Scattered words from a results page don't count
  as proof; a phrase from the page after the click does. The oracle now
  finishes by quoting its last action's result, and every task still passes —
  the gate costs a competent model nothing.
- **Stuck detection** (a repeat on an unchanged page earns a hint; three
  failures in a row, a re-plan with thinking on) and **a context budget**
  (the latest two results in full, older ones a line each, the oldest
  summarised; the prefix never changes, for the prompt cache).
- **Private work stays local**: anything touching `models.cloud_exclusions`,
  or reading mail, messages, contacts, files or the clipboard, runs on local
  models from then on.
- **Errands ask and resume.** A question from a background errand becomes the
  turn's pending question; the answer resumes the errand. "Stop" now reaches
  a foreground action in progress too.
- Budgets: `automation.max_steps` 50, `max_wall_s` 600, `max_model_calls` 80
  (replacing per-milestone steps).


## Phase 6 — Mac apps as a surface

**Web tasks, oracle: 42 / 42; every phrasing: 72 / 72**, unchanged, at the
same cost (5.5 model calls per task) — with a smaller toolkit: an errand is
now offered its surface's core tools, and the rest join only when the errand
mentions them (a download, an installer, a terminal command).

What changed:

- **App windows, fully.** v2's native tools searched a window's *direct
  children* through System Events — anything inside a toolbar, split view or
  scroll view was out of reach. The new native surface reads the whole
  Accessibility tree (bounded by depth, nodes and time), lists every control
  with an `[axN]` handle, names table rows by the text inside them, lists
  only a big table's visible rows, walks past scroll bars and layout
  containers, and puts a sheet or dialog — and its buttons — first.
- **Acting on handles:** `click_control` (the accessibility press where the
  control has one, a genuine click at its centre where it doesn't),
  `type_into`, `choose_option`, `choose_menu_item` by path, `drag_control`.
  A wrong menu path answers with what the menu does contain.
- **Genuine input:** the full key table (F-keys, forward delete, chords
  like cmd+shift+s), Unicode typing independent of keyboard layout, long text
  pasted with the clipboard put back exactly as it was.
- **Numbered marks** for windows that show Accessibility nothing: the window
  is photographed, its text read on-device (Vision), and everything worth
  pointing at numbered; a vision model can pick a number for an icon.
- **Safety:** the consequence check reads the real control ("Move to Trash",
  "Empty Trash…", "Erase", "Install", "Don't Save", "Shut Down" always ask,
  whatever the model called them); password fields are refused.
- The operator reads the window again after each app action, in the app it
  acted in.

This is tested against a fake accessibility tree (42 tests; each guard proven
by reverting it). `scripts/check_native.py` runs the real thing on a Mac.

