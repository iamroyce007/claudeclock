# Windowsill

A live countdown of your Claude **5-hour usage window**, in the macOS menu bar
and the Windows taskbar. It tells you when the window is about to lapse, and
opens the next one automatically when it does.

<img src="assets/icon.png" width="96" alt="Windowsill">

```
menu bar:   ● 3:42:17
```

**[Download for macOS (.dmg)](../../releases/latest)** ·
**[Download for Windows (.exe)](../../releases/latest)**

No Python, no terminal, no `pip install` — the app ships its own runtime.
There is also a full CLI if you prefer one.

---

## What it actually knows, and how

This is the part most tools in this space get wrong, so it is worth being
precise.

**Anthropic does expose the 5-hour window officially.** Two first-party
surfaces carry it, and this tool reads both:

| Source | What it is | Confidence |
|---|---|---|
| `oauth` | `GET https://api.anthropic.com/api/oauth/usage` — the same endpoint Claude Code's own `/usage` screen calls, authenticated with the OAuth token already on your machine | **authoritative** |
| `statusline` | The documented Claude Code `statusLine` hook, which receives `rate_limits.five_hour.{used_percentage, resets_at}` as JSON on stdin | **reported** |
| `local` | Inference from timestamps in Claude Code's own local transcripts (`~/.claude/projects/**/*.jsonl`) | **inferred** |

The endpoint returns exactly what we need:

```json
{
  "five_hour": {
    "utilization": 6.0,
    "resets_at": "2026-07-31T16:40:00.714082+00:00"
  },
  "limits": [
    {"kind": "session", "percent": 6, "is_active": true,
     "resets_at": "2026-07-31T16:40:00.714082+00:00"}
  ]
}
```

Note what it gives you: the **reset instant**, not the start. Since the window
is a fixed length, `session_start = resets_at - 5h` — exact, not estimated,
whenever the reading is authoritative. When `five_hour` is `null`, no window is
currently open, which is precisely the signal that the previous one has fully
reset.

The UI always labels which source answered and how much to trust it. A
countdown driven by local inference is never presented as if it came from the
server.

### On the constraints you set

No browser automation, no UI scraping, no undocumented endpoints. The usage
endpoint is a documented first-party API called with your own credentials; the
statusline contract is documented in Claude Code's own settings reference; the
re-arm prompt goes out through `claude -p`, the supported non-interactive CLI
entry point. Credentials are **read** from the store Claude Code already
maintains and never copied elsewhere.

### The one genuine limitation

A 5-hour window does not begin on a clock boundary — **it begins with your
first request after the previous window lapsed.** Nothing can observe the start
of a window that has not started yet.

So "automatically start the next window" necessarily means "send something."
That is what the re-arm step does: one tiny prompt (`"Hi"` by default) on the
cheapest model, through `claude -p`, roughly 15 seconds after the old window
lapses. It costs a negligible slice of the window it opens. Set
`SILL_AUTO_TRIGGER=false` (or run `sill run --no-trigger`) to observe only.

---

## Install

### The app (recommended)

Grab the latest [release](../../releases/latest):

* **macOS** — open `Windowsill-x.y.z.dmg`, drag the app to Applications, launch
  it. It appears in the menu bar with no Dock icon. The build is ad-hoc signed
  rather than notarised, so on first launch macOS will call it "unidentified":
  right-click the app, choose **Open**, confirm once.
* **Windows** — download `Windowsill.exe` and run it. It appears in the
  taskbar tray. SmartScreen will warn on first run for the same reason; choose
  **More info → Run anyway**.

The automatic re-arm needs the Claude Code CLI (`claude`) on your `PATH`.
Everything else works without it.

### From source

Requires Python 3.10+ and a working `claude` CLI on `PATH`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[gui]'
```

Windows (PowerShell):

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1; pip install -e ".[gui]"
```

The `[gui]` extra pulls in the menu bar / tray toolkit for your platform
(`rumps` on macOS, `pystray` + `Pillow` elsewhere). Plain `pip install -e .`
gives you the terminal tool with no GUI dependencies.

Then confirm everything is wired up:

```bash
sill check
```

That verifies credentials, hits the usage endpoint once, resolves the re-arm
command on `PATH`, and lists your notification channels.

---

## Use

```bash
sill tray                 # menu bar (macOS) / taskbar (Windows), live countdown
sill panel                # just the detail window, attaches to a running monitor
sill run                  # live dashboard, auto re-arm on (default command)
sill run --no-ui          # headless, for background/service use
sill run --no-trigger     # observe only, never send anything
sill status               # one-shot snapshot, then exit
sill status --json        # machine-readable, for scripts and status bars
sill check                # diagnostics
sill log --tail 30        # the event ledger
sill test-notify          # fire a test notification on every channel
sill trigger --dry-run    # show the exact re-arm command
```

`sill status --json` is the integration point if you want to drive something
else from this:

```json
{
  "state": "Active",
  "session_start": "2026-07-31T11:40:00.714082+00:00",
  "resets_at": "2026-07-31T16:40:00.714082+00:00",
  "remaining_seconds": 15157.3,
  "utilization_percent": 6.0,
  "source": "oauth",
  "confidence": "authoritative"
}
```

### Optional: the offline source

```bash
sill install-statusline
```

Writes a small shim and registers it as your Claude Code `statusLine`. The shim
tees the statusline JSON to a state file and prints a normal status line back,
so you keep a working statusline and gain a source that needs no network. It
backs up any existing `settings.json` and refuses to clobber a `statusLine` you
already have unless you pass `--force`.

---

## Menu bar and taskbar

```bash
sill tray
```

One command: it starts the monitor *and* the platform's native front-end.

**macOS** puts a live countdown directly in the menu bar — `● 4h12m` — updated
every second. The dot carries the urgency (green → amber → red → purple once
lapsed) so a glance is enough. Clicking it drops down status, session start,
reset time, limit used, and the source, plus actions: open the detail window,
send a re-arm prompt now, open the log folder, quit.

**Windows** has no menu bar text, so the countdown is drawn *into* the tray
icon: a ring showing how much of the window has elapsed with the hours or
minutes remaining in the middle, in the same urgency colours. Hovering gives
the full status; left-click opens the detail window.

Both open the same small detail window, which you can also launch on its own
with `sill panel` — a big countdown, a progress bar, session start, reset time,
and which source is answering. Escape dismisses it.

### How the pieces fit

The monitor writes a small JSON document (`live.json`) once a second; every
front-end reads it. That means exactly one process talks to Anthropic no matter
how many views are open, and — the part that actually matters — no GUI toolkit
ever shares a main thread with the scheduler.

That constraint drives the design. `rumps` runs an NSApplication loop on the
main thread and `pystray` runs its own icon loop; Tkinter demands a main thread
too. Hosting both in one process is the classic way these apps deadlock, so the
detail window is launched as a **separate process**. The countdown is also
extrapolated from the document's age, so the panel renders at 4 Hz off a 1 Hz
feed without the clock stepping.

If the monitor is not running, every front-end says so plainly rather than
showing a frozen clock — and it distinguishes "not running" from "running but
stalled" by checking whether the writing PID is still alive.

### Starting it at login

**macOS** — the same `launchd` plist shown below, with `run --no-ui` replaced
by `tray`.

**Windows** — Task Scheduler, "At log on", running
`.venv\Scripts\pythonw.exe -m sill tray` (note `pythonw`, so no console window
appears behind the tray icon).

---

## Configuration

Copy `.env.example` to `.env` and edit. Every setting is optional. The ones
worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `SILL_POLL_INTERVAL` | `60` | seconds between endpoint polls |
| `SILL_SOURCES` | `oauth,statusline,local` | priority order; first usable answer wins |
| `SILL_AUTO_TRIGGER` | `true` | send the re-arm prompt when the window lapses |
| `SILL_TRIGGER_PROMPT` | `Hi` | what gets sent |
| `SILL_TRIGGER_MODEL` | `claude-haiku-4-5-20251001` | cheapest available |
| `SILL_ALERT_THRESHOLDS` | `30,10,5` | minutes remaining, comma-separated |
| `SILL_DISCORD_WEBHOOK_URL` | — | optional |
| `SILL_SLACK_WEBHOOK_URL` | — | optional |
| `SILL_WEBHOOK_URL` | — | optional, generic JSON POST |
| `SILL_LOG_LEVEL` | `INFO` | `TRACE` for per-poll source detail |
| `SILL_LOG_JSON` | `false` | structured JSON lines |

State lives in `~/.windowsill/` by default:

```
monitor.log      rotating diagnostic log (5 MB × 5)
events.jsonl     append-only ledger: every reset, trigger, and session start
state.json       cross-restart state, so a restart mid-window is seamless
```

The ledger is the durable record, and it is `jq`-friendly:

```bash
jq -r 'select(.event=="window_reset") | .ts' ~/.windowsill/events.jsonl
```

---

## Notifications

Fire at 30, 10, and 5 minutes remaining (configurable), on window reset, and
when a new window opens.

- **macOS** — `terminal-notifier` if installed, otherwise `osascript`
- **Windows** — WinRT toast via PowerShell, falling back to a tray balloon
- **Linux** — `notify-send`
- **Discord / Slack / generic webhook** — set the URL and they are on

Each alert fires **once per window**, keyed by the window's reset instant. That
key is persisted, so restarting mid-window does not re-fire alerts you already
saw. If the machine sleeps through the 30- and 10-minute marks and wakes at 4
minutes, you get the 5-minute alert only — not a burst of three.

---

## Fault tolerance

The failure modes here are mostly boring, which is the goal.

**Network loss.** Exponential backoff with jitter, capped at
`SILL_BACKOFF_MAX`. The last known-good reset instant keeps driving the
countdown, and the UI marks itself `[stale]`. Nothing blanks out.

**The usage endpoint rate-limiting you.** It will 429 if polled aggressively
(a dozen calls in a few seconds is enough). That is treated as an ordinary
backoff case, the source is marked `rate limited (429)` in the footer, and the
countdown coasts. The 60-second default poll interval stays well clear of it;
there is no reason to lower it, since the countdown is computed locally and
repaints every second regardless.

**A weaker source disagreeing.** This is the subtle one. If the endpoint is
down and local inference guesses a window ninety minutes off, that is *not* a
new window — it is a worse answer to the same question. A lower-confidence
source may never redefine a window an authoritative one established; it coasts
on the known boundary and says so. Conversely, when a better source comes back
and overrules a guess, that is recorded as `window_corrected` rather than
`session_start`: the window never changed, only what we knew about it, so it
does not count a cycle or fire a "new window" notification.

**Sub-second jitter.** `resets_at` is recomputed per request and varies by up
to a second between calls, which is enough to cross a second boundary and
change the window's identity. Readings within two minutes of each other are
the same window, pinned to the first instant seen — so neither the window id
nor the displayed countdown jitters. This survives restarts.

**Token rotation.** A `401` triggers one credential re-read (Claude Code
rotates the token in place) and a retry. Token *refresh* is opt-in and off by
default — two processes writing one credential store is a race worth avoiding,
and the re-arm shell-out runs `claude` every ~5h, which refreshes it for free.
On macOS a refreshed token is held in memory only; the Keychain entry Claude
Code owns is never written.

**Sleep / resume.** Wall-clock and monotonic time are compared every poll. A
drift past `SILL_CLOCK_JUMP_THRESHOLD` means the machine was suspended (or
someone moved the clock), so the local anchor is dropped, a `system_resume`
event is logged, and the next poll re-syncs from a real source rather than
trusting elapsed time. APScheduler jobs use `coalesce=True`, so a 6-hour sleep
produces one catch-up poll, not 360.

**Claude Code restarts.** Irrelevant to the `oauth` source, which talks to the
server. The `statusline` source ages out readings older than 30 minutes rather
than reporting a window that stopped updating when you closed your last
session.

**A source throwing.** Caught per-source, logged with a traceback, and that
source is marked unhealthy in the footer. The others carry on.

**Re-arm already rate-limited.** If the window has not truly reset, `claude`
returns a usage-limit error; that is detected specifically and the retry loop
is abandoned rather than burning requests, leaving the next poll to
re-evaluate.

---

## Building the apps yourself

```bash
./packaging/make_dmg.sh          # macOS  -> dist/Windowsill-x.y.z.dmg
pyinstaller packaging/windowsill.spec   # Windows -> dist/Windowsill.exe
```

PyInstaller does not cross-compile, so the `.exe` has to be built on Windows.
[CI](.github/workflows/build.yml) builds both on every push and attaches them
to a release on a `v*` tag, which is where the download links point.

Both builds run a smoke test (`--diagnose`) before shipping. That check exists
because freezing a Python app fails in ways the source never does: py2app
points `SSL_CERT_FILE` at a deliberate placeholder, so the first HTTPS call
died with a bare `FileNotFoundError` until the entry point overrode it. If you
ever need to debug a packaged build:

```bash
/Applications/Windowsill.app/Contents/MacOS/Windowsill --diagnose
```

---

## Layout

```
src/cwm/
  cli.py                 argparse entry point and subcommands
  config.py              .env loading, validation, derived paths
  monitor.py             APScheduler orchestration, the run loop
  tracker.py             the window state machine
  sources/
    __init__.py          Snapshot, Source protocol, timestamp parsing
    oauth_usage.py       official usage endpoint (authoritative)
    statusline.py        statusLine hook source + shim (reported)
    local.py             transcript inference + clock-jump detection (inferred)
  auth.py                credential resolution (Keychain / file), refresh
  notify.py              desktop + Discord/Slack/webhook fan-out
  trigger.py             the `claude -p` re-arm, with retries
  ui.py                  Rich dashboard
  live.py                the per-second live-state channel + shared formatting
  gui/
    app.py               platform dispatch for the front-end
    macos.py             menu bar item (rumps)
    tray.py              taskbar/tray icon (pystray + Pillow)
    panel.py             the shared detail window (Tkinter)
  logging_setup.py       rotating structured logs + the event ledger
```

Adding a source means implementing `fetch() -> Snapshot | None` and registering
it in `build_sources`; the tracker ranks by `Confidence` and needs no other
changes.

---

## Running it in the background

**macOS** — `launchd`:

```xml
<!-- ~/Library/LaunchAgents/com.user.cwm.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.cwm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/full/path/to/.venv/bin/cwm</string>
    <string>run</string>
    <string>--no-ui</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.user.cwm.plist
```

**Windows** — Task Scheduler, "At log on", running
`.venv\Scripts\pythonw.exe -m sill run --no-ui` (note `pythonw`, so no console
window appears).

Idle cost is one HTTPS GET per minute and a one-second UI repaint; the poll
interval is the only thing worth tuning.

---

## License

MIT
