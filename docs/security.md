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

## Reporting a gap

If you find a way to make a HIGH-risk tool run without a confirmation, that is a
bug in the broker, not a configuration issue. The relevant code is
`backend/jarvis/security/permissions.py` and the gate in
`backend/jarvis/tools/registry.py::ToolRegistry.call`.
