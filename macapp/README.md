# JARVIS for the Mac

A native app around the JARVIS you already installed — not a browser tab.

* **Its own window.** The interface lives in a JARVIS window, not a tab. Close
  it and JARVIS keeps listening from the menu bar; click the Dock icon to bring
  it back.
* **Menu bar.** One click to show it, talk to it, restart it, read its log, or
  make it open at login.
* **⌥Space from anywhere.** A global shortcut brings JARVIS forward and starts
  push-to-talk (press again to send). It works whichever app is in front and
  needs no extra permission.
* **Notifications when you've tabbed away.** When JARVIS needs your OK (paying,
  sending, deleting), needs you to sign in somewhere, or finishes a task,
  macOS tells you. Click the notification to go straight there.
* **It looks after JARVIS.** The app starts the backend from your checkout (the
  same way `scripts/start.sh` does, so a `git pull` is picked up), stops it
  cleanly when you quit, and starts it again once if it stops unexpectedly.
* **First run without Terminal.** Open it on a fresh clone and it runs
  `scripts/setup.sh` for you in a window that shows what it's doing.

Nothing is frozen into the app: it's a small shell (a few hundred lines of
Swift) that runs `.venv/bin/jarvis` from your clone. Updating JARVIS is still
`git pull`; rebuild the app only when `macapp/` itself changes.

## Build it

You need Xcode, or just its command-line tools (`xcode-select --install`), on
macOS 13 or later.

```bash
cd macapp
./build.sh            # → build/JARVIS.app
open build/JARVIS.app # or move it to /Applications first
```

The first time, macOS may say it can't check the app: right-click it and choose
**Open**. The app is signed ad hoc, which is enough for your own Mac.
Distributing it to other people would need an Apple Developer ID and
notarisation, and that isn't part of this.

The app finds your clone by itself when it's built inside it (macapp/build),
or in `~/Jarvis`. Otherwise it asks once and remembers.

## Permissions

JARVIS runs as the app's child process, so macOS asks on **JARVIS.app**'s
behalf: microphone, Accessibility (to operate apps), Automation (Mail,
Calendar, your browser) and Screen Recording are granted to JARVIS.app, once,
instead of to Terminal. With an ad-hoc signature, macOS may ask again after
you rebuild the app.

API keys for the optional free cloud models: an app opened from Finder doesn't
see your shell's environment, so put them in the `.env` file in your clone (see
`.env.example`).

## Where things are

| | |
| --- | --- |
| Backend log | `~/Library/Logs/JARVIS/backend.log` (menu bar → Show Log) |
| JARVIS's own data | `~/JARVIS` — unchanged, shared with `start.sh` |
| The clone the app uses | remembered in the app's preferences; "Restart JARVIS" asks again if it's gone |

Only one JARVIS runs at a time: if you started one from Terminal, the app says
so. Quit that one first.

## How it works

| File | What it does |
| --- | --- |
| `Backend.swift` | finds the clone, runs `scripts/start.sh --no-browser --port <free port>` with a fresh session token, waits for `/api/health`, restarts once, stops with SIGTERM (SIGKILL after 5 s) |
| `MainWindow.swift` | the `WKWebView` window; tells the page it's in the app (`window.__JARVIS_NATIVE__`) and receives its messages; the microphone for JARVIS's own page only; other links open in your browser |
| `StatusMenu.swift` | the menu bar item, Open at Login (`SMAppService`), and the app's Edit menu (so copy and paste work) |
| `HotKey.swift` | the ⌥Space shortcut (Carbon hot keys — no Accessibility permission needed) |
| `Notifier.swift` | notifications, only while the JARVIS window isn't the one you're using |
| `SetupWindow.swift` | first-run `scripts/setup.sh`, with its output |
| `PowerObserver.swift` | watches thermal state and Low Power Mode, reports changes to the backend (`POST /api/system/power-state`) so the screen watcher can back off or pause under real pressure — see `backend/jarvis/core/power.py` |

The page's side is `frontend/src/lib/native.ts`: it passes on confirmation
requests and finished tasks, and exposes push-to-talk to the shortcut. In a
browser tab it does nothing.

CI builds the app on macOS for every change (the "Mac app" job), so it always
compiles. What CI can't do is run it with a microphone and your permissions.
That part is yours: build it, open it, and try ⌥Space.
