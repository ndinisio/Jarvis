"""The commit-ledger version of this checkout - the number in each commit's title.

``LEDGER_VERSION`` is the version the repository's latest commit carries as its title ("v8.58"). The
app shows it in the top left, so a glance says whether what is running is the latest code - which the
package version (``jarvis.__version__``, tracking toward a real 3.0.0) and a commit hash cannot, one
because it does not move per commit and the other because it means nothing without ``git log``.

It is set in the same commit as the work, every time, to that commit's title: ``scripts/ledger.py set
v8.59`` before committing (``scripts/ledger.py next`` says what is due). A commit-msg hook
(``git config core.hooksPath scripts/git-hooks``) refuses a commit whose title and this value differ, and
tests/test_ledger.py fails if this ever lags the latest commit's title. The frontend bakes the same value
into its build (frontend/vite.config.ts reads this file), so the interface can tell when it is older than
the backend it is talking to.
"""

LEDGER_VERSION = "v8.61"
