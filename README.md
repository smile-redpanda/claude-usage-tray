# Claude Usage Tray

A small Windows app that shows your Claude plan usage (the 5-hour and weekly limits) live, as a tray icon and a
floating always-on-top label above the taskbar.

```
⚡ 081% (03%)  ⏳ 03h43m (06m)  [weekly circles]  FIN
```

- **⚡ 081%** — 5-hour limit left (green / orange / red); **(03%)** how much it dropped last time.
- **⏳ 03h43m** — time until the 5-hour limit resets; **(06m)** how long that last drop took.
- **Weekly circles** — the weekly limit left, split into 7 parts (one per day). Teal = left, white = the part in use
  (the bar beside it shows how much of it is left), dark = used. A light gray square marks how many you *should*
  still have to last until the weekly reset.
- **FIN / DEV** — which Claude account is shown; click it to switch accounts.
- Leading zeros are gray, so the label keeps one width.

It also shows Windows notifications when the 5-hour usage passes 70% and when 30 minutes are left, and a
**🔑 Log in** button when the Claude Code login expires.

## Set up

Needs Windows and Python 3.14, and Claude Code signed in (the app uses its login).

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```
.venv\Scripts\pythonw.exe tray_app.py
```

`pythonw` runs without a console window (use `python.exe` to see errors). Right-click the tray icon for the menu:
show the label, choose or add an account, refresh, quit.

To build a single `.exe` that runs without Python: `powershell -ExecutionPolicy Bypass -File build_exe.ps1`
(output: `dist\ClaudeUsageTray.exe`).

## Claude Code extras

- `status_line.py` — a Claude Code status line (`⏳ 3h43m · 5h 9% · wk 0%`). In `~/.claude/settings.json`:
  `"statusLine": {"type": "command", "command": "<repo>/.venv/Scripts/python.exe <repo>/status_line.py",
  "refreshInterval": 60}`
- `skills/check-usage/` — a Claude Code skill to answer "how much usage do I have left?". Copy the folder to
  `~/.claude/skills/` to use it in every project.

## How it works

Every 5 minutes it asks `https://api.anthropic.com/api/oauth/usage` (undocumented; what Claude Code's `/usage`
screen uses) with the Claude Code login token, and saves the result to `~/.claude/usage-watch.json`. Anthropic may
change that address at any time.
