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
