---
name: check-usage
description: Read the user's Claude plan usage (5-hour and weekly limits) from usage-watch.json and report it. Use when the user asks how much usage they have left, when their limit resets, whether they should slow down, or invokes /check-usage.
---

# Check Claude plan usage

The user's current Claude plan usage is saved by the usage tray app (the `claude-usage-tray` repository,
`C:\ekk\repositories\claude-usage-tray`) to:

`C:\Users\USER\.claude\usage-watch.json`

## How to answer

1. Read that file with the Read tool. It looks like:

   ```json
   {
     "updatedAt": "2026-10-13T06:22:06.445028+00:00",
     "accountFolder": "C:\\Users\\USER\\.claude",
     "fiveHour": { "percentUsed": 9.0, "resetsAt": "2026-10-13T16:10:00.100797+00:00" },
     "weekly":   { "percentUsed": 0.0, "resetsAt": "2026-10-20T12:00:00.100825+00:00" }
   }
   ```

   - `fiveHour` — the rolling 5-hour limit; `weekly` — the weekly limit (all models). Either can be `null`.
   - `percentUsed` — 0 to 100.
   - `resetsAt` — when that limit resets, in UTC.
   - Compute time left from `resetsAt` and the current time (get it with `date`). If `resetsAt` has passed, that
     window has reset; the next one starts with the user's next message.
   - `updatedAt` — when the tray app last fetched live numbers (UTC; it fetches every 5 minutes). If it is more than
     ~15 minutes old, say the numbers may be out of date because the tray app is probably not running.
   - `accountFolder` — which Claude account the numbers are for: `C:\Users\USER\.claude` is "FIN"
     (fin.eakalak@gmail.com), `C:\Users\USER\.claude-dev` is "DEV" (dev.eakalak@gmail.com). Say which account it is.
     The file only holds the account the tray app currently follows (switched by clicking the name on its label).

2. Reply in a few short lines, using the user's local time (UTC+7) for reset times:

   ```
   5-hour limit: 11% used · resets in 3h 50m (at 17:40)
   Weekly limit: 63% used · resets in 5h 10m (at 19:00)
   Updated 2 min ago
   ```

3. Add one plain line of advice only when it matters:
   - 5-hour ≥ 80%: suggest keeping the remaining work small until it resets.
   - ≤ 30 min until the 5-hour reset: mention it resets soon.
   - Weekly ≥ 80%: mention the weekly limit is getting close.

## If the file is missing or empty

Say so plainly: it is written by the tray app (`C:\ekk\repositories\claude-usage-tray\tray_app.py`, started from
the desktop shortcut "Claude Usage Tray"), so the app is probably not running. Don't guess numbers.

Read-only: never edit or delete `usage-watch.json`; the tray app owns it.
