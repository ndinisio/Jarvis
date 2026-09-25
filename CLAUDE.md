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
  machine or the GitHub UI). Once they confirm it's landed, `claude/main-work`
  and `main` are the same commit, i.e. `claude/main-work` has *nothing* on it
  beyond `main` again — that's the natural reset, not a separate cleanup
  step, since the next commit just starts from there.
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
