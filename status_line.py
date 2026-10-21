"""Claude Code status line script.

Shows the live usage that tray_app.py saves to USAGE_JSON_FILE_PATH, as one short line at the bottom of Claude Code:
    ⏳ 3h48m · 5h 8% · wk 0%

It deliberately ignores the rate_limits Claude Code sends on stdin: those are only from that session's last reply
to Claude, so they go stale (and old sessions would overwrite fresh numbers).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

USAGE_JSON_FILE_PATH = Path.home() / ".claude" / "usage-watch.json"
OLD_DATA_WARNING_MINUTES = 5


def minutes_until(iso_time_text, now_utc):
    if not iso_time_text:
        return None
    target_time_utc = datetime.fromisoformat(iso_time_text.replace("Z", "+00:00"))
    return max(0, round((target_time_utc - now_utc).total_seconds() / 60))


def format_minutes_as_hours_and_minutes(total_minutes):
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"


def build_status_line_text():
    try:
        usage_snapshot = json.loads(USAGE_JSON_FILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return "usage: start the Claude Usage Tray app"

    now_utc = datetime.now(timezone.utc)
    five_hour = usage_snapshot.get("fiveHour") or {}
    weekly = usage_snapshot.get("weekly") or {}
    status_line_parts = []

    minutes_left_in_five_hour_window = minutes_until(five_hour.get("resetsAt"), now_utc)
    if minutes_left_in_five_hour_window == 0:
        status_line_parts.append("5h reset")
    elif five_hour:
        if minutes_left_in_five_hour_window is not None:
            status_line_parts.append(f"⏳ {format_minutes_as_hours_and_minutes(minutes_left_in_five_hour_window)}")
        status_line_parts.append(f"5h {five_hour['percentUsed']:.0f}%")

    if minutes_until(weekly.get("resetsAt"), now_utc) == 0:
        status_line_parts.append("wk reset")
    elif weekly:
        status_line_parts.append(f"wk {weekly['percentUsed']:.0f}%")

    minutes_since_update = round((now_utc - datetime.fromisoformat(usage_snapshot["updatedAt"])).total_seconds() / 60)
    if minutes_since_update > OLD_DATA_WARNING_MINUTES:
        status_line_parts.append(f"(old: {format_minutes_as_hours_and_minutes(minutes_since_update)}, is the tray app running?)")

    return " · ".join(status_line_parts)


def main():
    sys.stdin.read()  # Claude Code sends session info; not needed here.
    sys.stdout.reconfigure(encoding="utf-8")
    print(build_status_line_text())


if __name__ == "__main__":
    main()
