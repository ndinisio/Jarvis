# Working on this repository

## Branches

Two branches, and only two:

- **`main`** — the real, released line. Every commit here is something the
  user has explicitly approved landing.
- **`claude/main-work`** — the working branch. This is where Claude commits
  by default, the moment it does. (A bare branch named `claude` isn't
  possible here — the GitHub App this environment authenticates as can only
  create refs under the `claude/` prefix, so `claude/main-work` is the
  closest match to what was asked for.)

No other branches should exist. If one turns up (a leftover feature branch,
a stale rewrite scratch branch, an old `claude/<random-words>` session
branch), check whether its content is already on `main` (`git log
main..<branch>` and compare by content, not just by hash — a rebase changes
hashes without losing anything) and clean it up rather than letting it
linger. This environment's git credentials can create/update refs under
`claude/*` and fast-forward `main`, but cannot delete remote branches or
create tags directly — that needs the user to run
`git push origin --delete <branch>` themselves, or the GitHub UI. **Force-pushing
`main` is blocked outright by the harness's own safety layer** ("Git
Destructive"), not just by GitHub permissions — there is no way around this
from inside a session, and none should be attempted. Anything that requires
rewriting `main`'s existing history gets built and verified on
`claude/main-work`, then handed to the user with the exact command to run
themselves.

## Commit policy

- Commit to `claude/main-work` as soon as a genuine, complete unit of work
  is done — not mid-task, not a WIP snapshot, not something that doesn't
  build or pass its tests.
- **Never push to `main` unless the user explicitly asks.** `claude/main-work`
  is where things go straight away; `main` only receives work when told to.
- When the user says to push, bring `claude/main-work`'s accumulated commits
  onto `main` (fast-forward if possible) and push.
- Routine engineering hygiene still applies on `claude/main-work` even
  though it's the working branch: tests pass, linters are clean.

## Commit message format

- **Title: the version number only** (`v1.0`, `v1.1`, `v2.0`, …) — nothing
  else on that line.
- **Body: the explanation** — what changed and why, written the way commit
  bodies always have been in this repo (what changed, why, what it costs,
  what proves it works). The version-number title replaces what used to be
  a descriptive subject line; that description now opens the body instead
  of being lost.
- History starts at an **Initial commit** (unversioned — just scaffolding,
  e.g. a bare README), then **v1.0**.
- Every commit after that bumps the version from whatever the previous
  commit's title was:
  - **Small** — a bug fix, a verification/hardening pass, a docs or tooling
    addition, a compatibility patch — bumps the **minor** number
    (v1.0 → v1.1 → v1.2 …).
  - **Bigger** — a genuinely new capability, or a substantial rewrite of
    existing architecture — bumps the **major** number and resets minor to
    zero (v1.3 → v2.0; v9.0 → v10.0; there is no ceiling — "and so on").
  - The call is made on scope, not diff size: a new capability that touches
    few files is still major; a large mechanical refactor that changes
    nothing about what the product does is still minor.
- This numbering is the **commit-history ledger**, kept deliberately
  separate from the package's own `__version__` / `pyproject.toml` version
  (currently tracking toward a real `3.0.0` release) and from any git tags.
  The two will not match, and that's expected, not a bug to reconcile —
  don't try to make the commit-title version chase the package version or
  vice versa.
