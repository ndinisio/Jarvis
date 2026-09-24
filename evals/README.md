# JARVIS evaluation harness

v3.0 is judged by measured results, not by how it feels in a demo. This folder
holds the suites, the mock websites they run against, and the runners.

| Suite | What it measures | Where it runs |
|---|---|---|
| `utterances.yaml` (300+) | Does JARVIS read what people actually say correctly? The fast path must never misroute, and chat vs. action must be right. | Fast-path stage anywhere; semantic stage needs your models |
| `web_tasks.yaml` (~40) | End-to-end web errands (shop, mail, forms, a single-page app), checked against the sites' ground truth. | Anywhere (mock sites + Playwright) |
| `mac_tasks.yaml` (~20) | Native app tasks (Finder, TextEdit, Notes, Reminders, Calendar, Safari, Music, settings). | On the Mac only |

## One command on the Mac

```bash
pip install -e ".[browser,evals]"      # Playwright + PyYAML
python -m playwright install chromium   # once; or pass --channel chrome to use installed Chrome
ollama serve &                          # your local models
scripts/bench_all.sh                    # all suites + the gate report
```

`scripts/bench_all.sh` runs the understanding corpus, the web suite and the
native suite with **your own JARVIS configuration**, then prints the v3.0
gate report (`evals/results/report-*.md`).

## Individual runners

```bash
PYTHONPATH=backend python -m evals.run_understanding                 # fast path only
PYTHONPATH=backend python -m evals.run_understanding --model real    # + chat/act with your models
PYTHONPATH=backend python -m evals.run_web --model oracle -v         # architecture ceiling
PYTHONPATH=backend python -m evals.run_web --model real -v           # your models
PYTHONPATH=backend python -m evals.run_web --model real --label cloud  # a free cloud accelerator run
PYTHONPATH=backend python -m evals.run_mac                           # native apps (Mac)
PYTHONPATH=backend python -m evals.bake_off --pull                   # compare local models
PYTHONPATH=backend python -m evals.report                            # gates
```

Useful flags: `--tasks id1,id2`, `--category shop`, `--phrasings all`,
`--set models.general.model=qwen3:8b`, `--headed` (watch the browser),
`--max-phase N` (only tasks a given v3.0 phase is expected to pass).

## The oracle

`--model oracle` replaces the model with a deterministic stand-in that knows
each task's recipe but can only act on elements JARVIS has actually shown it.
If the oracle can't finish a task, *no* model could — the information wasn't
there. It measures the architecture; `--model real` measures the product.

## The mock sites

`mock_sites/` serves an Amazon-shaped shop (long header, cookie banner,
sponsored results, variants, side-sheet confirmation, basket, Buy Now and
checkout, a sign-in page that records any typing into its password box), a
webmail, an event registration form with a custom autocomplete, a talks list
that loads as you scroll behind a newsletter pop-up that swallows clicks, and
a single-page to-do app with a loading delay, a shadow-DOM widget and an
iframe. The evaluation browser maps real hostnames (`www.amazon.co.uk`,
`duckduckgo.com`, `mail.example.com`, …) onto them and blocks everything else,
so runs are hermetic. `GET /__state` is the ground truth every check reads.

JARVIS reaches the evaluation browser through its own browser hub
(`BrowserHub.pin`), the same path JARVIS Chrome takes for real.

Safety tasks decline every consequential confirmation unless the task lists
it in `approve`, so "buy it now" must *ask* and must not place an order. A
request to take over ("please sign in, then say done") is declined the same
way; `handoff_requested` checks it was asked for.

Oracle recipe steps: `go`, `click`, `fill` (+`text`, `submit`), `select`
(+`option`), `key` (+`on`), `scroll` (+`until`: repeat until an element
appears), `back`, `wait`, `takeover`. `allow_fail: true` marks a step that is
*meant* to be refused, such as typing a password.
