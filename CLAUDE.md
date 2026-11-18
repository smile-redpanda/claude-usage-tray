# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows tray app showing the user's Claude plan usage (5-hour and weekly limits), plus a Claude Code status line
and a skill. Moved here from the `fintek` repository (`C:\ekk\repositories\fintek`, where it was `src/usage_tray/`).
Remote: `https://github.com/ekkSoft/claude-usage-tray`. No tests or lint config.

- `.venv` (Python 3.14) with `requirements.txt` installed. Run with `.venv\Scripts\pythonw.exe tray_app.py` from the
  repo root. Both scripts run directly (no package, no `-m`).
- The desktop shortcut "Claude Usage Tray" starts it from this folder. After changing `tray_app.py`, restart it: stop
  the running `pythonw.exe ... tray_app.py` processes, then start it again.
- `build_exe.ps1` builds `dist\ClaudeUsageTray.exe` with PyInstaller (icon in `assets/`).

## tray_app.py

- Every 5 min a background thread fetches `https://api.anthropic.com/api/oauth/usage` (undocumented; what Claude
  Code's /usage uses) with header `anthropic-beta: oauth-2025-04-20` and the Claude Code login token from
  `<config folder>\.credentials.json` → `claudeAiOauth.accessToken`. **Never print or log the token.** It saves
  `{updatedAt, accountFolder, fiveHour: {percentUsed, resetsAt}, weekly: {...}}` to
  `C:\Users\USER\.claude\usage-watch.json`.
- 401 / missing token → `UsageLoginNeededError`: the label shows a `🔑 Log in` button (and the tray menu an item)
  that opens a console running `claude auth login` for the selected account; the user signs in themselves.
- 429 → wait the server's `Retry-After`. Fetching every 60 s got it rate limited for 9+ hours: keep ≥ 5 min.
- Label (tkinter, borderless, topmost, bottom-right above the taskbar, draggable, right-click → Hide / Quit), top to
  bottom: a weekly-limit-left bar in 7 day segments (Claude orange #d97757, the partly used day white); a 7-day segment bar for how much SHOULD be left (time until the weekly reset:
  whole days only, in gray: the day in progress and empty parts are not drawn; `SegmentBar`); the text row
  "⚡ 081% (03%)  ⏳ 03h43m (06m)  FIN"; a blue time-left bar and the usage-left bar, both in 5 segments (an hour / 20% each)
  (1 px gaps within each bar pair).
  Numbers have a fixed length so the width never changes; each text sits in a fixed-size slot frame
  (`put_in_slot` / `set_slot_text`); leading zeros are drawn gray (`LeadingZerosGrayText`). No drop yet → gray
  "(00%)" / "(00s)" placeholders. Drop time is 2 digits + s/m/h.
- Each successful fetch also appends a line to `~/.claude/usage-history.jsonl` (`at`, `accountFolder`,
  `fiveHourPercentUsed`, `fiveHourResetsAt`; kept 24 h). Hovering over the "⚡ 081% (03%)" text or the usage-left bar
  pops up `UsageHistoryGraph` above the label: 5-hour usage left over the last 10 h (two windows), one column per
  fetch in the usage colors, gaps > 10 min left empty, dashed blue lines at window resets, hours along the bottom.
- Accounts: the default login (`~/.claude`) plus extra ones from `~/.claude/usage-tray-settings.json`
  (`defaultAccountName`, `extraAccounts` [{name, folder}], `selectedAccountName`). The user's: "fin"
  (fin.eakalak@gmail.com, `~/.claude`) and "dev" (dev.eakalak@gmail.com, `~/.claude-dev`, i.e. `CLAUDE_CONFIG_DIR`).
  Tray menu → Account picks one or adds one. On the label the selected account's (upper-case) name is a button (red
  unless logged in); it opens `AccountPanel` above the label: click a row to select it (closes the panel); per account a button by its
  login state (`read_login_state`, from `.credentials.json` expiry times only: `expiresAt` / `refreshTokenExpiresAt`,
  in ms): "in" → "Log out" (console running `claude auth logout`), "expired" (access token ran out, refresh token
  still good, or the last fetch got 401) → "Renew" (hidden `claude auth status`, which makes Claude Code renew the
  token), "out" → "Log in" (console running `claude auth login`); all with that account's `CLAUDE_CONFIG_DIR`. An
  account not used in Claude Code for hours shows "expired": its token lasts ~8 h. Each row also shows
  the time left in that account's 5-hour window from its latest line in `usage-history.jsonl` ("1h 25m left" / "n/a", right-aligned).
  The
  email comes from `.claude.json` (no token in it). Snapshots of another account (by `accountFolder`) are ignored.
- Tray icon shows the 5-hour % (green < 70, orange ≥ 70, red ≥ 90). Notifications once per 5-hour window at 70%
  and 30 min left (remembered in `~/.claude/usage-tray-shown-warnings.json`). Single instance via a named mutex.
  System DPI awareness (per-monitor made it land on the second monitor); placement corrections are logged to
  `~/.claude/usage-tray-placement.log`.

## status_line.py

Claude Code status line (`~/.claude/settings.json` → `statusLine`, `refreshInterval: 60`): prints
`⏳ 3h43m · 5h 9% · wk 0%` from `usage-watch.json`. It ignores the `rate_limits` on stdin (stale per session).

## skills/check-usage

The source of the user-level skill installed at `~/.claude/skills/check-usage/` (answers "how much usage is left?"
from `usage-watch.json`). After editing it here, copy it there again.

## Verifying changes

The user wants changes verified before they are called done: run the app (or a small script that builds
`FloatingUsageLabel` / `UsageTrayApp` with a fake `fetch_live_usage_snapshot`), check the label with
`PIL.ImageGrab` (call `ctypes.windll.shcore.SetProcessDpiAwareness(1)` first or the grab misses), then restart the
real app.
