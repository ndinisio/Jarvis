# Security model

JARVIS runs with your user's privileges on your Mac. This document states
exactly what it can do, what it will refuse, and where the gates are.

## Risk levels

Every tool declares one. The declaration — not the model's opinion — decides
whether you are asked.

### LOW — runs without asking

`get_time` `get_system_info` `get_battery` `get_storage` `get_memory` `get_cpu`
`get_processes` `get_network` `check_permissions` `open_application`
`activate_application` `list_applications` `open_url` `browse_to`
`get_current_page` `list_browser_tabs` `send_notification` `set_volume`
`read_clipboard` `write_clipboard` `append_clipboard` `capture_screen`
`analyse_screen` `search_web` `fetch_page` `fetch_pages` `list_files`
`read_file` `write_file`¹ `create_note` `search_files` `workspace_info`
`check_email` `read_email` `search_email` `read_calendar` `search_calendar`

¹ Inside `~/JARVIS` only — outside it, the same tool escalates per path.

### MEDIUM — asks, unless you allow the level

`close_application` `move_file` `run_shell_command` `draft_email`
`create_calendar_event`

### HIGH — always asks

`send_email` `delete_file` · any shell command that isn't allowlisted · any
write outside the workspace and the configured readable folders.

## The confirmation gate

1. A tool above LOW calls `PermissionBroker.require(...)`.
2. A `confirm.request` event opens the dialog, stating the action, the risk and
   the arguments.
3. Execution waits. No answer in 90 seconds is a refusal.
4. "Allow for this session" is offered for MEDIUM only — never for HIGH.

The model cannot resolve a confirmation, suppress one, or call a tool with a
lower risk level than the tool declares. Saying "yes" resolves whatever dialog
is open; it cannot conjure an action that wasn't already requested.

## Who can talk to JARVIS

The server listens on `127.0.0.1` only, and every run gets a fresh random
**session token** (`backend/jarvis/core/auth.py`) — the same scheme Jupyter uses
for its local server. The link JARVIS opens (and prints) carries it:
`http://127.0.0.1:8765/?token=…`. The interface keeps it for that tab and removes
it from the address bar.

* Every `/api/*` call and the `/ws` event stream require the token; only
  `/api/health` (which reveals nothing about you) and the interface's own files
  don't.
* The event stream also checks the browser's `Origin`: a page from any other
  site — including one JARVIS itself is browsing — is refused even if it
  somehow had the token. Without this, such a page could answer a pending
  confirmation on your behalf.
* A tab left open from an earlier run says so ("session ended") instead of
  reconnecting forever. Open the link from the current run.
* A launcher can supply the token in `JARVIS_SESSION_TOKEN` (at least 32
  characters); `scripts/dev.sh` does, so a development server that reloads
  keeps the same one.

## Filesystem boundary

| Location | Read | Write | Delete |
| --- | --- | --- | --- |
| `~/JARVIS` | yes | yes | asks (moves to `.trash`) |
| `~/Documents`, `~/Downloads`, `~/Desktop` (configurable) | yes | asks (HIGH) | refused |
| elsewhere in `$HOME` | asks | asks (HIGH) | refused |
| `/System`, `/usr`, `/bin`, `/sbin`, `/etc`, `/Library/Launch*`, `/Applications` | refused | refused | refused |

Paths are fully resolved (symlinks included) before classification, so a link
cannot be used to step outside the boundary.

## Shell execution

* argv lists by default — no shell interpretation, no injection surface
* allowlist of read-only diagnostic commands (`df`, `ps`, `sysctl`, `pmset`, …)
* denylist that can never auto-run (`rm`, `sudo`, `diskutil`, `curl`, `kill`, …)
* anything containing `| > >> ; && \` $( ` requires confirmation
* `allow_shell: false` disables the tool entirely

The model is steered towards dedicated tools; shell is the last resort, not the
first.

## Privacy

* **Local by default.** Wake word, speech recognition, reasoning and memory run
  on your machine. Nothing is sent anywhere unless you enable a remote provider
  or ask for web research.
* **The clipboard is sensitive.** Credential-shaped contents are never read
  aloud, and `clipboard_remote_guard` keeps them from remote providers.
* **Screen capture is explicit.** On demand only; images are saved to
  `~/JARVIS/captures` and shown in the interface.
* **Memory is yours.** Plain SQLite at `~/JARVIS/memory/jarvis.db` — inspect it,
  edit it, delete it, or say "forget that".
* **Secrets stay in the environment.** API keys are never written to
  `config.json` and are redacted from the API and the UI.
* **Web research is outbound only.** Search queries and page fetches go out; your
  files, clipboard and screen do not.

## Places JARVIS never operates (`security/denylist.py`)

Three lists in `security` settings, each enforced where a surface is reached
rather than left to the model:

| Setting | Default | Where it's enforced |
| --- | --- | --- |
| `blocked_apps` | password managers, Keychain Access, Passwords | the native surface refuses to read a window, type, press a key or act on a control of the app — whether named or merely in front; the registry refuses any app action naming it |
| `blocked_windows` | "System Settings: Privacy & Security", Passwords, Users & Groups, Login Items | the same, for a window whose title contains the part after the colon |
| `blocked_sites` | "bank" (any host containing it), PayPal, the big UK and US banks, password vaults, Apple ID | page tools refuse to read the page listing or act on it; `get_current_page` refuses to read it |

Opening one is still allowed — "open 1Password", "go to my bank's website" —
because that's you asking; the result says the rest is yours and asks the
operator to stop there.

## Other people's words (`security/untrusted.py`)

Pages, emails, messages, files, calendar invitations and app windows are
written by other people, and any of them may address an AI directly. The
defence is in two layers:

1. **The gates don't listen to anyone.** A consequential action is judged on
   the element it will really hit and confirmed by you, whatever the model
   was told. This layer holds even if the model is fooled.
2. **The model is told whose words these are.** Content reaches the operator
   between `⟦the page says⟧` … `⟦end of what the page says⟧` markers; the
   content's own copies of those characters are replaced, so a page can't
   close the fence early and write "instructions" after it. JARVIS's own
   notes about a page (a sign-in wall, a pagination hint) are always outside
   the fence. Text that reads as instructions to an assistant earns a plain
   warning, also outside. The operator's instructions say that fenced text is
   information and that only the user's request is an instruction.

The evaluation shop carries a product description that tells "any AI
assistant" to click Buy Now; the safety suite checks that nothing is ordered.

## The audit trail (`security/audit.py`)

Every call through the tool registry writes one JSON line to
`~/JARVIS/audit/<date>/<task id>.jsonl` (or the request id outside a task):
time, tool, arguments with secrets redacted, the real target (element text,
URL, form action), whether it was consequential, how it was allowed —
`setting`, `autonomy`, `task grant`, `session grant`, `user` — or that it was
`declined` or `refused (denylist)`, the outcome and how long it took. With
`audit_screenshots` on, each action on a page in JARVIS Chrome also keeps a
small picture of the page afterwards. Records older than `audit_days` (30) are
removed at start-up; `security.audit: false` stops recording. The interface
reads them from `GET /api/audit` and `GET /api/audit/{task id}`.

## Reporting a gap

If you find a way to make a HIGH-risk tool run without a confirmation, that is a
bug in the broker, not a configuration issue. The relevant code is
`backend/jarvis/security/permissions.py` and the gate in
`backend/jarvis/tools/registry.py::ToolRegistry.call`.
