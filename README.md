# ClaudeClock

A live countdown of your Claude **5-hour usage window**, in the macOS menu bar
and the Windows taskbar.

When the window runs out, ClaudeClock sends a single tiny prompt (`"Hi"`) to
open the next one, so your next session is always ready.

![ClaudeClock](assets/screenshot.png)

**[Download for macOS (.dmg)](../../releases/latest)** ·
**[Download for Windows (.exe)](../../releases/latest)**

---

## What it does

- Shows the exact time left in your current 5-hour window, ticking every second.
- Reads the real reset time from Anthropic's own usage endpoint — not a guess.
- Warns you at 30, 10, and 5 minutes remaining (desktop notification, plus
  optional Discord/Slack/webhook).
- When the window ends, sends `"Hi"` through the Claude Code CLI to start the
  next window, then tracks that one. Repeats indefinitely.
- Logs every reset, prompt sent, and session start to `~/.claudeclock/events.jsonl`.

Colours tell you the state at a glance: terracotta while you have time, amber
under 30 minutes, red under 5, purple once the window has lapsed.

---

## Where the data comes from

| Source | What it is | Accuracy |
|---|---|---|
| `oauth` | `GET https://api.anthropic.com/api/oauth/usage` — the same endpoint Claude Code's own `/usage` screen uses, authenticated with the token already on your machine | exact |
| `statusline` | The Claude Code `statusLine` hook, which reports `rate_limits.five_hour` | exact, but only while Claude Code is open |
| `local` | Inferred from Claude Code's local transcript timestamps | approximate, fallback only |

The first one that answers wins, and the app always labels which one it used.
A 5-hour window starts on your *first request* after the last one ended — which
is why the app sends `"Hi"` to open a new one rather than waiting.

---

## Setup

### 1. Install Claude Code

ClaudeClock needs the `claude` CLI to send the re-arm prompt.

**macOS / Linux**

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://claude.ai/install.ps1 | iex
```

Then sign in — this is what gives ClaudeClock access to your usage data:

```bash
claude
```

Type `/login` if it does not prompt you automatically, and follow the browser
flow. Confirm it worked:

```bash
claude --version
```

### 2. Install ClaudeClock

**macOS**

1. Download `ClaudeClock-1.0.0.dmg` from [Releases](../../releases/latest).
2. Open it and drag **ClaudeClock** into Applications.
3. Launch it from Applications.
4. macOS will say the developer is unidentified — right-click the app, choose
   **Open**, then confirm. This is only needed once.

**Windows**

1. Download `ClaudeClock.exe` from [Releases](../../releases/latest).
2. Run it. SmartScreen will warn on first run — choose **More info → Run anyway**.

That's it. Look at the **top-right of your screen** (macOS menu bar) or the
**system tray** (Windows). There is no Dock icon by design.

### 3. Check it is working

```bash
claude --version
```

<a name="from-source"></a>

### Run from source

This path involves no signed binary at all, so it sidesteps SmartScreen,
antivirus heuristics and Gatekeeper entirely:

```bash
git clone https://github.com/iamroyce007/claudeclock.git
cd claudeclock
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui]"
cclock check
```

`cclock check` verifies your login, hits the usage endpoint once, and confirms
the `claude` command can be found:

```
Credentials       OK  keychain, plan=pro
Usage endpoint    OK  window resets 2026-07-31 22:10:00, 40.0% used
Re-arm command    OK  /Users/you/.local/bin/claude -p Hi --output-format json
Notifications     OK  desktop
```

---

## "Windows blocked this" / "Trojan detected" / firewall prompt

If Windows SmartScreen blocks the `.exe`, your antivirus flags it as a trojan,
or a firewall prompt appears on first run — that is expected, and it is a
**false positive**. Here is exactly why, and what to do.

### Why it happens

Nothing about the app is malicious, but four of its properties are also true of
real malware, and heuristic scanners cannot tell the difference:

| Property | Why it looks suspicious |
|---|---|
| **Unsigned** | No Authenticode signature, so SmartScreen has zero reputation for it and warns by default |
| **PyInstaller one-file** | It unpacks a Python runtime into `%TEMP%` at launch. Self-extracting-and-executing is textbook dropper behaviour |
| **Reads credentials** | It reads the OAuth token Claude Code already stored, to ask Anthropic about your usage |
| **Network + subprocess** | It calls `api.anthropic.com` and launches `claude` to send the re-arm prompt |

Brand-new files with few downloads also have no reputation history, which alone
is enough for SmartScreen to interrupt.

### What has already been done to reduce it

- **UPX compression removed.** Executable packers are far more common in
  malware than in legitimate software; a packed *and* unsigned binary is close
  to a guaranteed detection. Dropping it costs a few MB and removes one of the
  strongest heuristic triggers.
- **Built in public.** Every release is compiled by
  [GitHub Actions](.github/workflows/build.yml) from the tagged source in this
  repository — not on anyone's laptop. You can read the workflow, read the
  source, and rebuild it yourself.

### What to do

**1. SmartScreen — "Windows protected your PC"**

Click **More info**, then **Run anyway**. Once per version.

**2. Antivirus quarantined it**

Windows Security → **Virus & threat protection** → **Protection history** →
find ClaudeClock → **Actions** → **Allow on device**.

To pre-empt it, add an exclusion for the file you downloaded:

```powershell
Add-MpPreference -ExclusionPath "$env:USERPROFILE\Downloads\ClaudeClock.exe"
```

Only exclude the specific file, never the whole Downloads folder.

**3. Firewall prompt on first run**

Allow it on **private networks**. It needs outbound HTTPS to
`api.anthropic.com` to read your usage window; it opens no inbound ports and
listens on nothing.

### Verify before you trust it

Do not take the above on faith. Check the SHA-256 of your download against the
`SHA256SUMS.txt` published with each release:

```powershell
Get-FileHash .\ClaudeClock.exe -Algorithm SHA256
```

```bash
shasum -a 256 ClaudeClock-*.dmg
```

If they match, the binary is bit-for-bit what CI built from the tagged source.
You can also skip the binaries entirely and [run from source](#from-source) —
no signing, no SmartScreen, no antivirus heuristics involved.

### The actual fix

These warnings only disappear with **code signing**, which is a paid identity
check, not a code change:

- **Windows** — an OV (~$200/yr) or EV (~$400/yr) code-signing certificate. EV
  grants immediate SmartScreen reputation; OV builds it up over time.
- **macOS** — an Apple Developer ID ($99/yr) plus notarisation, which is what
  removes *"Apple could not verify this app is free of malware"*.

Until those exist, every fresh download will warn. That is a distribution
problem, and no amount of work on the app changes it.

### macOS equivalent

If macOS says **"ClaudeClock Not Opened — Apple could not verify..."** with only
a *Move to Trash* button, the app is quarantined because the browser flagged
the download. Clear that flag on this one app:

```bash
xattr -dr com.apple.quarantine /Applications/ClaudeClock.app
```

This affects only that app; it does not weaken Gatekeeper system-wide.

---

## Commands

Only needed if you installed from source; the app needs none of these.

```bash
cclock tray            # menu bar / taskbar app
cclock panel           # just the detail window
cclock status          # print the current window and exit
cclock status --json   # machine-readable
cclock check           # diagnose login, endpoint, and re-arm command
cclock log --tail 20   # every reset and prompt sent
cclock trigger         # send the "Hi" prompt right now
```

---

## Configuration

Optional. Copy `.env.example` to `.env` and edit.

| Setting | Default | What it does |
|---|---|---|
| `CLAUDECLOCK_AUTO_TRIGGER` | `true` | send `"Hi"` when the window ends |
| `CLAUDECLOCK_TRIGGER_PROMPT` | `Hi` | what gets sent |
| `CLAUDECLOCK_TRIGGER_MODEL` | `claude-haiku-4-5-20251001` | cheapest model |
| `CLAUDECLOCK_ALERT_THRESHOLDS` | `30,10,5` | minutes-remaining warnings |
| `CLAUDECLOCK_POLL_INTERVAL` | `60` | seconds between checks |
| `CLAUDECLOCK_THEME` | `dark` | `dark`, `light`, or `auto` |
| `CLAUDECLOCK_DISCORD_WEBHOOK_URL` | — | optional |
| `CLAUDECLOCK_SLACK_WEBHOOK_URL` | — | optional |

Logs and state live in `~/.claudeclock/`.

---

## Start it at login

- **macOS** — System Settings → General → Login Items → add ClaudeClock.
- **Windows** — put a shortcut to `ClaudeClock.exe` in `shell:startup`.

---

## Authors

- **[@iamroyce007](https://github.com/iamroyce007)** — author and maintainer
- Built with [Claude Code](https://claude.com/claude-code)

## License

MIT
