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
branch), don't assume it's redundant just because it's old: check whether
its content is already reachable from `main` by *content*, not just by
hash — a rewrite changes hashes without losing anything, so compare tree
hashes / diffs, not `git log main..<branch>` alone, which lies about
renamed/rewritten history. Genuinely unique work found this way (it has
happened — a real bug fix sitting on an otherwise-stale branch) gets folded
into a normal commit on `claude/main-work` like anything else, not
cherry-picked wholesale. Once a branch's content is confirmed fully
captured, clean it up rather than letting it linger.

This environment's git credentials can create/update refs under `claude/*`,
but **cannot push to `main` at all** — not a force-push, not even a plain
fast-forward. The harness's own Auto Mode safety layer blocks every push to
`main` outright, regardless of GitHub-level permissions and regardless of
whether the update is destructive; there is no way around this from inside
a session, and none should be attempted (retrying via another tool, a
differently-shaped command, or a later turn is exactly what this rule
forbids). It also can't delete remote branches or create tags directly —
that needs the user to run `git push origin --delete <branch>` themselves,
or use the GitHub UI. So getting anything onto `main` — fast-forward or
not — is always the user's own action: build and verify it on
`claude/main-work`, then hand them the exact command
(`git push origin claude/main-work:main`, plus `--force` only on the rare
occasion the histories have actually diverged) to run from their own
machine or the GitHub UI.

## Commit policy

- Commit to `claude/main-work` as soon as a genuine, complete unit of work
  is done — not mid-task, not a WIP snapshot, not something that doesn't
  build or pass its tests.
- **Never ask the user to approve a push to `main`, and never attempt one —
  it always fails from inside a session (see above).** `claude/main-work`
  is where things go straight away; getting something from there onto
  `main` is a step the user physically has to run themselves, every time.
- When the user says to push, don't just describe the command — give the
  exact one to run (`git push origin claude/main-work:main`, from their own
  machine or the GitHub UI), **and in the same message give the exact
  command to delete `claude/main-work`** (`git push origin --delete
  claude/main-work`) — this environment can do neither, so both are always
  theirs to run, and handing over only the first one leaves the branch
  lingering until asked twice. Once both are run, the working branch is
  gone outright, not just reset to zero — the next unit of work recreates
  it fresh from `main`'s new tip.
- Routine engineering hygiene still applies on `claude/main-work` even
  though it's the working branch: tests pass, linters are clean.
- The working branch is `claude/main-work`, not a bare `claude-working-branch`
  — the GitHub App this environment authenticates as only accepts new refs
  under the `claude/` prefix (see above), so this is the closest match to
  any differently-named working branch that gets asked for.

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
  - **Almost always** — a bug fix, a verification/hardening pass, a docs or
    tooling addition, a compatibility patch, a performance or safety pass,
    UI work, a rewrite of existing architecture (however substantial), or a
    new capability assembled from tools JARVIS already has — bumps the
    **minor** number (v7.0 → v7.1 → v7.2 → … → v7.13 → …, no ceiling). This
    is the default outcome for a code change; reach for major only when the
    case below is clearly met.
  - **Rarely** — bringing in genuinely new *software*: a new third-party
    integration or engine JARVIS didn't talk to before (HomeKit, the Kokoro
    TTS engine), or a new artifact the product ships as (the native Mac app
    shell) — bumps the **major** number and resets minor to zero
    (v6.5 → v7.0; no ceiling — "and so on"). A smarter automation loop, a
    new internally-built watcher or capability, deeper native-app control —
    all built from what JARVIS already has — stay minor; only a genuinely
    new outside system or a new distribution form earns major.
  - Because major is now reserved for that narrow case, expect the major
    number to stay small for a long time — around v7–v10 for a good
    while — not climb with every release the way minor does.
- This numbering is the **commit-history ledger**, kept deliberately
  separate from the package's own `__version__` / `pyproject.toml` version
  (currently tracking toward a real `3.0.0` release) and from any git tags.
  The two will not match, and that's expected, not a bug to reconcile —
  don't try to make the commit-title version chase the package version or
  vice versa.
