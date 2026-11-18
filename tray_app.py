"""Windows taskbar (system tray) app that shows your Claude plan usage.

Every 5 minutes it fetches your live usage from Anthropic (the same undocumented address Claude Code's /usage screen
uses, signed in with your Claude Code login), saves it to USAGE_JSON_FILE_PATH, and shows:
  - a small floating label (always on top, drag it anywhere): "⚡ 053% (03%)  ⏳ 03h21m (06m)" (% of the 5-hour limit
    left and its last drop, in the usage color; time in blue), and the account name;
    above the text the weekly limit left (Claude orange) and how much should be left by now (gray); below the text a blue
    time-left bar, then a green / orange / red usage-left bar at the bottom; hovering over the usage shows a graph
    of the 5-hour usage left over the last 10 hours (from USAGE_HISTORY_FILE_PATH, one line per fetch)
  - an icon with the 5-hour usage %, colored green / orange / red
  - hover text with time left, 5-hour %, weekly %, and when it was last updated
  - a Windows notification, once per 5-hour window, when usage crosses 70% and when 30 minutes are left

Run it from the repository root:
    .venv/Scripts/pythonw.exe tray_app.py
(pythonw has no console window; use python.exe instead to see errors.)
"""

import ctypes
import ctypes.wintypes
import json
import os
import re
import subprocess
import threading
import tkinter
import tkinter.font
import tkinter.simpledialog
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystray
import math

from PIL import Image, ImageDraw, ImageFont, ImageTk

USAGE_JSON_FILE_PATH = Path.home() / ".claude" / "usage-watch.json"
# One line per fetch, for the graph shown when hovering over the usage on the label.
USAGE_HISTORY_FILE_PATH = Path.home() / ".claude" / "usage-history.jsonl"
USAGE_HISTORY_KEEP_HOURS = 24
HISTORY_GRAPH_HOURS = 10  # two 5-hour windows
SHOWN_WARNINGS_FILE_PATH =Path.home() / ".claude" / "usage-tray-shown-warnings.json"
PLACEMENT_LOG_FILE_PATH =Path.home() / ".claude" / "usage-tray-placement.log"
# Claude Code's own settings folder; another account lives in its own folder (Claude Code's CLAUDE_CONFIG_DIR).
DEFAULT_CLAUDE_CONFIG_FOLDER = Path.home() / ".claude"
DEFAULT_ACCOUNT_NAME = "main"
# The extra accounts added from the tray menu, and which account the display follows.
TRAY_SETTINGS_FILE_PATH = Path.home() / ".claude" / "usage-tray-settings.json"
# Undocumented address that Claude Code itself uses for the /usage screen; Anthropic may change it.
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Every 5 minutes: asking every minute got the app rate limited (HTTP 429) for hours. The display still redraws every
# REFRESH_INTERVAL_SECONDS, so the countdown keeps moving between fetches.
FETCH_INTERVAL_SECONDS = 5 * 60
REFRESH_INTERVAL_SECONDS = 30
# When fetching keeps failing and the saved numbers are older than this, the label shows the error instead.
SHOW_ERROR_ON_LABEL_AFTER_MINUTES = 15
WARN_WHEN_MINUTES_UNTIL_RESET_AT_OR_BELOW = 30
HIGH_USAGE_WARNING_PERCENTAGE = 70
ICON_SIZE_PIXELS = 64
BAR_HEIGHT_PIXELS = 5
EXPECTED_BAR_HEIGHT_PIXELS = 3  # the gray weekly "should be left" bar, thinner than the others
TIME_LEFT_COLOR = "#5aaaff"  # blue: the time-left bar and the label text
# A thin space between "⚡" / "⏳" and the number; a normal space looked too far apart.
ICON_GAP = "\u2009"
# Numbers on the label have a fixed length ("081%", "03h43m", "(06m)"), so its width never changes; the leading zeros
# are drawn in this dim gray so "081%" still reads as 81%.
LEADING_ZEROS_COLOR = "#5a5a5a"
# The two weekly bars above the text, each split into 7 day segments.
WEEKLY_PART_COUNT = 7
FIVE_HOUR_PART_COUNT = 5  # the time-left and usage-left bars below the text: an hour / 20% per segment
WEEKLY_LEFT_FULL_DAY_COLOR = "#d97757"  # Claude orange: a whole day's share of the weekly limit still left
WEEKLY_LEFT_PART_DAY_COLOR = "#ffffff"  # white: the day's share that is partly used
WEEKLY_EXPECTED_LEFT_FULL_DAY_COLOR = "#8a8a8a"  # gray: a whole day that should be left, in the bar above the text
LABEL_BACKGROUND_COLOR = "#222222"
# The account button at the right end of the label, and the account panel it opens.
ACCOUNT_BUTTON_COLOR = "#3a3a3a"
ACCOUNT_BUTTON_HOVER_COLOR = "#4a4a4a"
ACCOUNT_BUTTON_TEXT_COLOR = "#dddddd"
ACCOUNT_LOGGED_OUT_TEXT_COLOR = "#ff6b6b"  # red: the account is signed out
LOGIN_BUTTON_COLOR = "#3a6ea5"
MINI_CLOCK_SIZE_PIXELS = 100  # the mini clock's width and height at 100% Windows scaling
MINI_CLOCK_RIM_COLOR = "#9a9a9a"  # light enough that the round clock stands out on dark windows
DRAG_HANDLE_COLOR ="#5a5a5a"  # the grip of dots at the label's left end
DRAG_HANDLE_HOVER_COLOR = "#cccccc"
ALERT_TEXT_COLOR ="#ffb347"  # orange: a problem shown in the account panel
ALERT_BACKGROUND_COLOR = "#3a2a14"
FIVE_HOUR_WINDOW_MINUTES = 5 * 60
STAY_ON_TOP_CHECK_MILLISECONDS = 250
# Windows API constants for placing the label.
GA_ROOT = 2
SPI_GETWORKAREA = 0x0030
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


class UsageFetchError(Exception):
    """A short, user-facing reason the live usage could not be fetched."""


class UsageLoginNeededError(UsageFetchError):
    """Signed out, or the login expired: the label shows a "Log in" button."""


class UsageServerBusyError(UsageFetchError):
    """HTTP 429: the usage server asked us to wait before asking again."""

    def __init__(self, seconds_to_wait):
        self.seconds_to_wait = seconds_to_wait
        try_again_local_time = datetime.now() + timedelta(seconds=seconds_to_wait)
        super().__init__(f"server busy, next try {try_again_local_time:%H:%M}")


def read_tray_settings():
    """{"extraAccounts": [{"name": "work2", "folder": "C:/Users/USER/.claude-work2"}], "selectedAccountName": "main"}"""
    try:
        return json.loads(TRAY_SETTINGS_FILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tray_settings(tray_settings):
    TRAY_SETTINGS_FILE_PATH.write_text(json.dumps(tray_settings, indent=2), encoding="utf-8")


def list_claude_accounts():
    """The default Claude Code login first (named "main", or the settings' "defaultAccountName"), then the accounts
    added from the tray menu."""
    tray_settings = read_tray_settings()
    default_account = {
        "name": tray_settings.get("defaultAccountName", DEFAULT_ACCOUNT_NAME),
        "folder": str(DEFAULT_CLAUDE_CONFIG_FOLDER),
    }
    return [default_account] + tray_settings.get("extraAccounts", [])


def read_account_email(claude_config_folder):
    """The signed-in email, which Claude Code saves next to its login (no password or token in that file).

    For the default folder it is in ~/.claude.json; with CLAUDE_CONFIG_DIR it is inside that folder."""
    if Path(claude_config_folder) == DEFAULT_CLAUDE_CONFIG_FOLDER:
        account_info_file_path = Path.home() / ".claude.json"
    else:
        account_info_file_path = Path(claude_config_folder) / ".claude.json"
    try:
        account_info = json.loads(account_info_file_path.read_text(encoding="utf-8"))
        return (account_info.get("oauthAccount") or {}).get("emailAddress")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def fetch_live_usage_snapshot(claude_config_folder):
    """Ask Anthropic for the current plan usage, signed in with the Claude Code login kept in claude_config_folder, in
    usage-watch.json's shape."""
    try:
        claude_login = json.loads((Path(claude_config_folder) / ".credentials.json").read_text(encoding="utf-8"))[
            "claudeAiOauth"
        ]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise UsageLoginNeededError("no Claude login found")
    if not claude_login.get("accessToken"):
        # Claude Code clears the token when it signs out; asking the server without one only wastes a request.
        raise UsageLoginNeededError("signed out")

    usage_request = urllib.request.Request(
        ANTHROPIC_USAGE_URL,
        headers={
            "Authorization": f"Bearer {claude_login['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-usage-tray",
        },
    )
    try:
        with urllib.request.urlopen(usage_request, timeout=15) as usage_response:
            usage_reply = json.loads(usage_response.read())
    except urllib.error.HTTPError as http_error:
        if http_error.code == 429:
            # Rate limited: wait as long as the server says (Retry-After, in seconds), and never less than usual.
            try:
                seconds_to_wait = int(http_error.headers.get("Retry-After", ""))
            except ValueError:
                seconds_to_wait = FETCH_INTERVAL_SECONDS * 2
            raise UsageServerBusyError(max(seconds_to_wait, FETCH_INTERVAL_SECONDS))
        if http_error.code == 401:
            # The login token expires; Claude Code renews it in the credentials file when it next runs.
            raise UsageLoginNeededError("login expired")
        raise UsageFetchError(f"usage server error {http_error.code}")
    except (urllib.error.URLError, TimeoutError):
        raise UsageFetchError("no internet / usage server unreachable")

    def convert_window(raw_window):
        if not raw_window or raw_window.get("utilization") is None:
            return None
        return {"percentUsed": round(raw_window["utilization"], 1), "resetsAt": raw_window.get("resets_at")}

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        # Which account these numbers belong to, so switching accounts never shows the other account's numbers.
        "accountFolder": str(claude_config_folder),
        "fiveHour": convert_window(usage_reply.get("five_hour")),
        "weekly": convert_window(usage_reply.get("seven_day")),
    }


def save_usage_snapshot(usage_snapshot):
    """Write via a temporary file and rename, so readers never see a half-written file."""
    temporary_file_path = USAGE_JSON_FILE_PATH.with_suffix(".tmp")
    temporary_file_path.write_text(json.dumps(usage_snapshot, indent=2), encoding="utf-8")
    temporary_file_path.replace(USAGE_JSON_FILE_PATH)


def read_usage_snapshot():
    """Return the saved usage as a dict, or None if the file is missing or half-written."""
    try:
        return json.loads(USAGE_JSON_FILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_usage_history():
    """Every saved fetch (oldest first), as dicts; lines that can't be read are skipped."""
    try:
        history_lines = USAGE_HISTORY_FILE_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    history_entries = []
    for history_line in history_lines:
        try:
            history_entry = json.loads(history_line)
            history_entry["atTime"] = datetime.fromisoformat(history_entry["at"])
            history_entries.append(history_entry)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    return history_entries


def append_usage_history(usage_snapshot):
    """Add one line per fetch to USAGE_HISTORY_FILE_PATH (for the hover graph), dropping lines older than
    USAGE_HISTORY_KEEP_HOURS so the file stays small (~300 lines a day per account)."""
    five_hour = usage_snapshot.get("fiveHour") or {}
    new_entry = {
        "at": usage_snapshot["updatedAt"],
        "accountFolder": usage_snapshot.get("accountFolder"),
        "fiveHourPercentUsed": five_hour.get("percentUsed"),
        "fiveHourResetsAt": five_hour.get("resetsAt"),
    }
    oldest_kept_time = datetime.now(timezone.utc) - timedelta(hours=USAGE_HISTORY_KEEP_HOURS)
    kept_entries = [
        {key: value for key, value in history_entry.items() if key != "atTime"}
        for history_entry in read_usage_history()
        if history_entry["atTime"] >= oldest_kept_time
    ]
    temporary_file_path = USAGE_HISTORY_FILE_PATH.with_suffix(".tmp")
    temporary_file_path.write_text(
        "".join(json.dumps(history_entry) + "\n" for history_entry in kept_entries + [new_entry]), encoding="utf-8"
    )
    temporary_file_path.replace(USAGE_HISTORY_FILE_PATH)


def minutes_until(iso_time_text, now_utc):
    """Minutes from now until an ISO time, recomputed live so the countdown keeps moving."""
    if not iso_time_text:
        return None
    target_time_utc = datetime.fromisoformat(iso_time_text.replace("Z", "+00:00"))
    return max(0, round((target_time_utc - now_utc).total_seconds() / 60))


def format_minutes_as_hours_and_minutes(total_minutes):
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"


def format_minutes_fixed_length(total_minutes):
    """Always "00h00m" (e.g. "03h05m"), for the label, where the width must not change."""
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}h{minutes:02d}m"


def format_duration_fixed_length(total_minutes):
    """Two digits and one unit: "45s" under a minute, "06m" under an hour, "01h" from an hour."""
    total_seconds = int(total_minutes * 60)
    if total_seconds < 60:
        return f"{total_seconds:02d}s"
    if total_minutes < 60:
        return f"{int(total_minutes):02d}m"
    return f"{min(99, int(total_minutes // 60)):02d}h"


def split_off_leading_zeros(text):
    """Split "⚡ 081%" into ("⚡ ", "0", "81%"): the text before the number, its leading zeros (with any units between
    them, e.g. "00h0" in "00h05m"), and the rest. The last digit always stays in the rest, so 0 shows as "00" + "0%".
    A text without digits is all "before"."""
    first_digit_position = next((position for position, character in enumerate(text) if character.isdigit()), None)
    if first_digit_position is None:
        return text, "", ""
    first_nonzero_digit_position = next(
        (position for position in range(first_digit_position, len(text)) if text[position] in "123456789"), None
    )
    if first_nonzero_digit_position is None:
        first_nonzero_digit_position = max(
            position for position, character in enumerate(text) if character.isdigit()
        )
    return (
        text[:first_digit_position],
        text[first_digit_position:first_nonzero_digit_position],
        text[first_nonzero_digit_position:],
    )


class LeadingZerosGrayText(tkinter.Frame):
    """A one-line text like a tkinter Label, except its leading zeros are drawn gray (see split_off_leading_zeros).
    It answers cget("font"/"text"), configure(text=..., foreground=...) and bind() like a Label, so the label's
    fixed-width slots and drag handling work with it unchanged."""

    def __init__(self, parent, text="", font=None, foreground="#bbbbbb", background="#222222"):
        super().__init__(parent, background=background)
        self.text_font = font
        self.text = text
        self.foreground = foreground
        # Three labels side by side, without padding, so they read as one text: before, gray zeros, rest.
        self.part_labels = [
            tkinter.Label(self, font=font, background=background, padx=0, borderwidth=0) for _ in range(3)
        ]
        for part_label in self.part_labels:
            part_label.pack(side="left")
        self.redraw()

    def cget(self, option):
        if option == "font":
            return self.text_font
        if option == "text":
            return self.text
        return super().cget(option)

    def configure(self, text=None, foreground=None, **frame_options):
        if text is not None:
            self.text = text
        if foreground is not None:
            self.foreground = foreground
        if frame_options:
            super().configure(**frame_options)
        self.redraw()

    def bind(self, sequence=None, func=None, add=None):
        # Clicks land on the part labels, not the frame behind them.
        for part_label in self.part_labels:
            part_label.bind(sequence, func, add)
        return super().bind(sequence, func, add)

    def redraw(self):
        before_text, zeros_text, rest_text = split_off_leading_zeros(self.text)
        for part_label, part_text, part_color in zip(
            self.part_labels,
            (before_text, zeros_text, rest_text),
            (self.foreground, LEADING_ZEROS_COLOR, self.foreground),
        ):
            part_label.configure(text=part_text, foreground=part_color)


def pick_usage_bar_color_for_percent(percent_used):
    """Brighter versions of the icon colors, readable on the label's dark background."""
    if percent_used is None:
        return "#bbbbbb"
    if percent_used >= 90:
        return "#ff6b6b"
    if percent_used >= 70:
        return "#ff9a3c"  # orange
    return "#7ee2a0"


def pick_color_for_percent(percent_used):
    if percent_used is None:
        return (128, 128, 128)  # gray: no data yet
    if percent_used >= 90:
        return (220, 50, 50)  # red
    if percent_used >= 70:
        return (240, 120, 30)  # orange
    return (40, 170, 80)  # green


def draw_icon_image(percent_used):
    """A colored square with the 5-hour % written on it ("?" when there is no data)."""
    icon_image = Image.new("RGBA", (ICON_SIZE_PIXELS, ICON_SIZE_PIXELS), (0, 0, 0, 0))
    drawing = ImageDraw.Draw(icon_image)
    drawing.rounded_rectangle(
        (0, 0, ICON_SIZE_PIXELS - 1, ICON_SIZE_PIXELS - 1), radius=12, fill=pick_color_for_percent(percent_used)
    )
    icon_text = "?" if percent_used is None else str(round(percent_used))
    try:
        font = ImageFont.truetype("arialbd.ttf", 40 if len(icon_text) <= 2 else 30)
    except OSError:
        font = ImageFont.load_default()
    drawing.text((ICON_SIZE_PIXELS / 2, ICON_SIZE_PIXELS / 2), icon_text, fill="white", font=font, anchor="mm")
    return icon_image


# For SegmentBar's part_color: draw the part-filled segment in the full color too.
SAME_AS_FULL_COLOR = "same as full"


class SegmentBar:
    """A thin bar the width of the floating label, split into segment_count segments (1 px gaps), filled from the
    left: whole segments in full_color, the part-filled one in part_color (not drawn when None), filled as far as it
    goes. Empty parts show a dark track, or nothing with show_empty_track=False. Used for the weekly bars (7 days)
    and the 5-hour bars (5 hours)."""

    def __init__(
        self, tk_root, segment_count, full_color, part_color=SAME_AS_FULL_COLOR, gap_below_pixels=0,
        show_empty_track=True, height_pixels=BAR_HEIGHT_PIXELS,
    ):
        self.segment_count = segment_count
        self.show_empty_track = show_empty_track
        self.full_color = full_color
        self.part_color = part_color
        self.height_pixels = height_pixels
        self.filled_fraction = None
        self.canvas = tkinter.Canvas(
            tk_root,
            width=1,  # let the text set the label's width; the bar stretches to match
            height=height_pixels,
            background=LABEL_BACKGROUND_COLOR,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="x", pady=(0, gap_below_pixels))
        self.canvas.bind("<Configure>", lambda event: self.redraw())

    def show_fraction(self, filled_fraction, full_color=None):
        """full_color changes the color (the usage bar turns orange / red)."""
        self.filled_fraction = filled_fraction
        if full_color is not None:
            self.full_color = full_color
        self.redraw()

    def redraw(self):
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        part_color = self.full_color if self.part_color == SAME_AS_FULL_COLOR else self.part_color
        segments_filled = (
            0 if self.filled_fraction is None else max(0.0, min(1.0, self.filled_fraction)) * self.segment_count
        )
        for segment_number in range(self.segment_count):
            left = round(width * segment_number / self.segment_count)
            right = round(width * (segment_number + 1) / self.segment_count)
            if segment_number < self.segment_count - 1:
                right -= 1  # the gap
            if self.show_empty_track:
                self.canvas.create_rectangle(left, 0, right, self.height_pixels, fill="#3a3a3a", width=0)
            fraction_of_segment_filled = max(0.0, min(1.0, segments_filled - segment_number))
            if fraction_of_segment_filled >= 1:
                color = self.full_color
            elif fraction_of_segment_filled > 0 and part_color is not None:
                color = part_color
            else:
                continue
            filled_right = left + round((right - left) * fraction_of_segment_filled)
            self.canvas.create_rectangle(left, 0, filled_right, self.height_pixels, fill=color, width=0)


class UsageHistoryGraph:
    """A popup above the label, like a phone's battery graph: the 5-hour usage LEFT over the last HISTORY_GRAPH_HOURS,
    one column per fetch (colored like the usage bar), with dashed blue lines where a 5-hour window reset."""

    def __init__(self, tk_root):
        self.tk_root = tk_root
        self.popup_window = tkinter.Toplevel(tk_root)
        self.popup_window.withdraw()
        self.popup_window.overrideredirect(True)
        self.popup_window.attributes("-topmost", True)
        # Sizes are for 100% Windows scaling; the app is DPI aware, so grow them with the screen's scaling.
        self.scale = tk_root.winfo_fpixels("1i") / 96
        self.width = round(380 * self.scale)
        self.height = round(150 * self.scale)
        self.canvas = tkinter.Canvas(
            self.popup_window,
            width=self.width,
            height=self.height,
            background=LABEL_BACKGROUND_COLOR,
            highlightthickness=1,
            highlightbackground="#444444",
        )
        self.canvas.pack()

    def show_above(self, label_window, history_entries):
        self.draw(history_entries, datetime.now(timezone.utc))
        self.popup_window.update_idletasks()
        popup_width = self.popup_window.winfo_reqwidth()
        popup_height = self.popup_window.winfo_reqheight()
        popup_left = label_window.winfo_rootx() + label_window.winfo_width() - popup_width
        popup_top = label_window.winfo_rooty() - popup_height - 4
        if popup_top < 0:  # the label was dragged to the top of the screen: show it below instead
            popup_top = label_window.winfo_rooty() + label_window.winfo_height() + 4
        self.popup_window.geometry(f"+{max(0, popup_left)}+{popup_top}")
        self.popup_window.deiconify()
        self.popup_window.lift()

    def hide(self):
        self.popup_window.withdraw()

    def is_showing(self):
        return self.popup_window.state() != "withdrawn"

    def draw(self, history_entries, now_utc):
        scale = self.scale
        canvas = self.canvas
        canvas.delete("all")
        small_font = ("Segoe UI", 8)
        dim_text_color = "#999999"
        plot_left = round(8 * scale)
        plot_right = self.width - round(36 * scale)
        plot_top = round(22 * scale)
        plot_bottom = self.height - round(20 * scale)
        graph_start_utc = now_utc - timedelta(hours=HISTORY_GRAPH_HOURS)
        graph_seconds = HISTORY_GRAPH_HOURS * 3600

        def x_for_time(time_utc):
            seconds_from_start = (time_utc - graph_start_utc).total_seconds()
            return plot_left + (plot_right - plot_left) * max(0.0, min(1.0, seconds_from_start / graph_seconds))

        def y_for_percent_left(percent_left):
            return plot_bottom - (plot_bottom - plot_top) * max(0.0, min(100.0, percent_left)) / 100

        canvas.create_text(
            plot_left, round(11 * scale), text=f"5-hour usage left · last {HISTORY_GRAPH_HOURS} h", anchor="w",
            font=("Segoe UI", 8, "bold"), fill="#bbbbbb",
        )
        # Every 2 hours (on the local hour): a faint line and the hour below it, like "15  17  19".
        local_now = datetime.now()
        first_tick_local = (local_now - timedelta(hours=HISTORY_GRAPH_HOURS)).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        tick_local = first_tick_local
        while tick_local <= local_now:
            if tick_local.hour % 2 == 0:
                tick_x = x_for_time(tick_local.astimezone(timezone.utc))
                canvas.create_line(tick_x, plot_top, tick_x, plot_bottom, fill="#303030")
                canvas.create_text(
                    tick_x, plot_bottom + round(10 * scale), text=f"{tick_local.hour:02d}", font=small_font,
                    fill=dim_text_color,
                )
            tick_local += timedelta(hours=1)
        # 100% / 50% / 0% lines with their numbers on the right.
        for percent_left in (100, 50, 0):
            line_y = y_for_percent_left(percent_left)
            canvas.create_line(plot_left, line_y, plot_right, line_y, fill="#3a3a3a", dash=(2, 3) if percent_left == 50 else None)
            canvas.create_text(
                plot_right + round(4 * scale), line_y, text=f"{percent_left}%", anchor="w", font=small_font,
                fill=dim_text_color,
            )

        graph_entries = [
            history_entry for history_entry in history_entries
            if history_entry["atTime"] >= graph_start_utc - timedelta(minutes=10)
        ]
        # One column per fetch, from its time to the next fetch (at most 10 minutes: a longer gap means the app or
        # computer was off, and stays empty).
        for entry_number, history_entry in enumerate(graph_entries):
            percent_used = history_entry.get("fiveHourPercentUsed")
            if percent_used is None:
                continue
            column_start = history_entry["atTime"]
            column_end = min(column_start + timedelta(minutes=10), now_utc)
            if entry_number + 1 < len(graph_entries):
                column_end = min(column_end, graph_entries[entry_number + 1]["atTime"])
            column_left = x_for_time(column_start)
            column_right = max(column_left + 1, x_for_time(column_end))
            canvas.create_rectangle(
                column_left, y_for_percent_left(100 - percent_used), column_right, plot_bottom,
                fill=pick_usage_bar_color_for_percent(percent_used), width=0,
            )
        # Where each 5-hour window ended (its reset time), so the two windows can be told apart.
        reset_times_utc = set()
        for history_entry in graph_entries:
            if history_entry.get("fiveHourResetsAt"):
                reset_time_utc = datetime.fromisoformat(history_entry["fiveHourResetsAt"].replace("Z", "+00:00"))
                reset_times_utc.add((reset_time_utc + timedelta(seconds=30)).replace(second=0, microsecond=0))
        for reset_time_utc in reset_times_utc:
            if graph_start_utc <= reset_time_utc <= now_utc:
                reset_x = x_for_time(reset_time_utc)
                canvas.create_line(reset_x, plot_top, reset_x, plot_bottom, fill=TIME_LEFT_COLOR, dash=(3, 2))

        if not any(history_entry.get("fiveHourPercentUsed") is not None for history_entry in graph_entries):
            canvas.create_text(
                (plot_left + plot_right) / 2, (plot_top + plot_bottom) / 2,
                text="No history yet: a point is added every 5 minutes.", font=small_font, fill=dim_text_color,
            )


class AccountPanel:
    """The popup the account button opens, above the label: one row per account ("● FIN  fin@…  [Log out]"), click a
    row to select it; "Log in" / "Log out" per account; × or Esc closes it."""

    def __init__(self, tk_root, on_select_account, on_login, on_logout, on_renew):
        self.tk_root = tk_root
        self.on_renew = on_renew
        self.on_select_account = on_select_account
        self.on_login = on_login
        self.on_logout = on_logout
        self.shown_account_rows = None
        self.popup_window = tkinter.Toplevel(tk_root)
        self.popup_window.withdraw()
        self.popup_window.overrideredirect(True)
        self.popup_window.attributes("-topmost", True)
        self.popup_window.configure(background="#444444")  # shows as a 1 px border around the content
        self.popup_window.bind("<Escape>", lambda event: self.hide())
        self.content_frame = tkinter.Frame(self.popup_window, background=LABEL_BACKGROUND_COLOR, padx=8, pady=6)
        self.content_frame.pack(padx=1, pady=1)

    def is_showing(self):
        return self.popup_window.state() != "withdrawn"

    def hide(self):
        self.popup_window.withdraw()

    def show_above(self, label_window, account_rows, alert_texts=()):
        self.rebuild(account_rows, alert_texts)
        self.popup_window.update_idletasks()
        popup_left = label_window.winfo_rootx() + label_window.winfo_width() - self.popup_window.winfo_reqwidth()
        popup_top = label_window.winfo_rooty() - self.popup_window.winfo_reqheight() - 4
        if popup_top < 0:
            popup_top = label_window.winfo_rooty() + label_window.winfo_height() + 4
        self.popup_window.geometry(f"+{max(0, popup_left)}+{popup_top}")
        self.popup_window.deiconify()
        self.popup_window.lift()
        self.popup_window.focus_force()

    def update_if_changed(self, account_rows, alert_texts=()):
        """Called every second while open, so a login or logout finishing in its console shows up by itself."""
        if self.is_showing() and (account_rows, list(alert_texts)) != self.shown_account_rows:
            self.rebuild(account_rows, alert_texts)

    def rebuild(self, account_rows, alert_texts=()):
        """account_rows: [{"name", "email", "loginState" ("in" / "expired" / "out"), "isSelected",
        "usageText", "usageColor"}]; alert_texts: problems to show under the rows in orange, e.g. two accounts
        showing the same usage."""
        self.shown_account_rows = (account_rows, list(alert_texts))
        for old_widget in self.content_frame.winfo_children():
            old_widget.destroy()
        background = LABEL_BACKGROUND_COLOR
        header_row = tkinter.Frame(self.content_frame, background=background)
        header_row.grid(row=0, column=0, columnspan=5, sticky="we", pady=(0, 4))
        tkinter.Label(
            header_row, text="Accounts", font=("Segoe UI", 9, "bold"), foreground="#bbbbbb", background=background
        ).pack(side="left")
        close_button = tkinter.Label(
            header_row, text="×", font=("Segoe UI", 10, "bold"), foreground="#999999", background=background,
            cursor="hand2",
        )
        close_button.pack(side="right")
        close_button.bind("<Button-1>", lambda event: self.hide())

        for row_number, account_row in enumerate(account_rows, start=1):
            row_background = "#2e2e2e" if account_row["isSelected"] else background
            is_logged_in = account_row["loginState"] == "in"
            name_color = ACCOUNT_BUTTON_TEXT_COLOR if is_logged_in else ACCOUNT_LOGGED_OUT_TEXT_COLOR
            row_cells = [
                tkinter.Label(
                    self.content_frame, text="●" if account_row["isSelected"] else "○", font=("Segoe UI", 9),
                    foreground=TIME_LEFT_COLOR if account_row["isSelected"] else "#777777", background=row_background,
                    padx=4,
                ),
                tkinter.Label(
                    self.content_frame, text=account_row["name"].upper(), font=("Segoe UI", 9, "bold"),
                    foreground=name_color, background=row_background, anchor="w", padx=4,
                ),
                tkinter.Label(
                    self.content_frame,
                    text={"in": account_row["email"] or "?", "expired": "login expired"}.get(
                        account_row["loginState"], "signed out"
                    ),
                    font=("Segoe UI", 8), foreground="#999999" if is_logged_in else name_color,
                    background=row_background, anchor="w", padx=4,
                ),
                # Time left in its 5-hour window: "1h 25m left", or "n/a", right-aligned.
                tkinter.Label(
                    self.content_frame, text=account_row["usageText"], font=("Segoe UI", 8),
                    foreground=account_row["usageColor"], background=row_background, anchor="e", padx=6,
                ),
            ]
            for column_number, row_cell in enumerate(row_cells):
                row_cell.grid(row=row_number, column=column_number, sticky="nswe", pady=1)
                row_cell.configure(cursor="hand2")
                row_cell.bind(
                    "<Button-1>", lambda event, account_name=account_row["name"]: self.on_select_account(account_name)
                )
            if is_logged_in:
                action_text, action_color, action = "Log out", ACCOUNT_BUTTON_COLOR, self.on_logout
            elif account_row["loginState"] == "expired":
                # Only the short-lived token ran out: Claude Code renews it from the saved login, no browser needed.
                action_text, action_color, action = "Renew", LOGIN_BUTTON_COLOR, self.on_renew
            else:
                action_text, action_color, action = "Log in", LOGIN_BUTTON_COLOR, self.on_login
            action_button = tkinter.Label(
                self.content_frame, text=action_text, font=("Segoe UI", 8, "bold"), foreground="white",
                background=action_color, padx=8, cursor="hand2", width=7,
            )
            action_button.grid(row=row_number, column=4, sticky="e", padx=(8, 0), pady=1)
            action_button.bind(
                "<Button-1>", lambda event, account_name=account_row["name"], action=action: action(account_name)
            )

        for alert_number, alert_text in enumerate(alert_texts):
            tkinter.Label(
                self.content_frame, text=f"⚠ {alert_text}\n    the numbers may be wrong (same login in both?)",
                font=("Segoe UI", 8, "bold"), foreground=ALERT_TEXT_COLOR, background=ALERT_BACKGROUND_COLOR,
                anchor="w", justify="left", padx=6, pady=2,
            ).grid(row=len(account_rows) + 1 + alert_number, column=0, columnspan=5, sticky="we", pady=(4, 0))


def draw_mini_clock_image(
    size_pixels, time_left_fraction, usage_left_fraction, usage_color, account_name, account_name_color
):
    """The minimized display: a round clock face whose full circle is the 5-hour window. Hour ticks (5) and 10-minute
    ticks; the long blue needle points at the time left, the short one (usage color) at the usage left (a full turn =
    100%); the account name in the middle. Drawn 4x larger and shrunk, so the
    edges are smooth. Returns an RGBA image (transparent outside the circle)."""
    supersample = 4
    big_size = size_pixels * supersample
    image = Image.new("RGBA", (big_size, big_size), (0, 0, 0, 0))
    drawing = ImageDraw.Draw(image)
    center = big_size / 2
    radius = big_size / 2 - supersample

    def point_at(angle_fraction, distance):
        """Clockwise from 12 o'clock: angle_fraction 0..1 of a full turn."""
        angle = angle_fraction * 2 * math.pi
        return center + distance * math.sin(angle), center - distance * math.cos(angle)

    # Two rings: Claude orange on the outside, then the light gray rim just inside it.
    drawing.ellipse(
        (center - radius, center - radius, center + radius, center + radius),
        fill=WEEKLY_LEFT_FULL_DAY_COLOR,
    )
    orange_ring_width = 2 * supersample
    drawing.ellipse(
        (center - radius + orange_ring_width, center - radius + orange_ring_width,
         center + radius - orange_ring_width, center + radius - orange_ring_width),
        fill=LABEL_BACKGROUND_COLOR, outline=MINI_CLOCK_RIM_COLOR, width=2 * supersample,
    )
    # Ticks: every 10 minutes a short dim one, every hour a long bright one (300 minutes = a full turn).
    for minute in range(0, FIVE_HOUR_WINDOW_MINUTES, 10):
        is_hour = minute % 60 == 0
        tick_length = radius * (0.16 if is_hour else 0.07)
        drawing.line(
            (point_at(minute / FIVE_HOUR_WINDOW_MINUTES, radius * 0.9 - tick_length),
             point_at(minute / FIVE_HOUR_WINDOW_MINUTES, radius * 0.9)),
            fill="#bbbbbb" if is_hour else "#5a5a5a", width=(2 if is_hour else 1) * supersample,
        )

    def draw_needle(fraction, length, color, width):
        tip = point_at(max(0.0, min(1.0, fraction)), length)
        drawing.line((center, center, *tip), fill=color, width=width)
        tip_radius = width / 2
        drawing.ellipse((tip[0] - tip_radius, tip[1] - tip_radius, tip[0] + tip_radius, tip[1] + tip_radius), fill=color)

    if time_left_fraction is not None:
        draw_needle(time_left_fraction, radius * 0.8, TIME_LEFT_COLOR, 3 * supersample)
    if usage_left_fraction is not None:
        draw_needle(usage_left_fraction, radius * 0.55, usage_color, 5 * supersample)

    if account_name:
        try:
            name_font = ImageFont.truetype("segoeuib.ttf", round(radius * 0.3))
        except OSError:
            name_font = ImageFont.load_default()
        drawing.text(
            (center, center), account_name.upper(), font=name_font, anchor="mm", fill=account_name_color,
            stroke_width=round(1.5 * supersample), stroke_fill=LABEL_BACKGROUND_COLOR,
        )
    return image.resize((size_pixels, size_pixels), Image.LANCZOS)


class MiniClock:
    """The minimized display: a round borderless always-on-top window showing draw_mini_clock_image. Drag it from
    anywhere; a click without dragging calls on_click (back to the full label)."""

    # The color Windows makes see-through, so only the round face shows. Nearly black, so the smoothed edge pixels
    # (a mix of the face and this color) still look like part of the dark rim.
    TRANSPARENT_COLOR = "#010101"

    def __init__(self, tk_root, on_click, on_right_click, on_moved):
        self.on_click = on_click
        self.on_moved = on_moved
        self.window = tkinter.Toplevel(tk_root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.9)
        self.window.attributes("-transparentcolor", self.TRANSPARENT_COLOR)
        self.size_pixels = round(MINI_CLOCK_SIZE_PIXELS * tk_root.winfo_fpixels("1i") / 96)
        self.canvas = tkinter.Canvas(
            self.window, width=self.size_pixels, height=self.size_pixels, background=self.TRANSPARENT_COLOR,
            highlightthickness=0, borderwidth=0, cursor="fleur",
        )
        self.canvas.pack()
        self.canvas_image = self.canvas.create_image(0, 0, anchor="nw")
        self.photo_image = None  # keep a reference, or tkinter drops the picture
        self.press_position = None
        self.drag_start_offset = (0, 0)
        self.is_dragging = False
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", on_right_click)
        self.shown_drawing_inputs = None

    def draw(self, time_left_fraction, usage_left_fraction, usage_color, account_name, account_name_color):
        drawing_inputs = (
            None if time_left_fraction is None else round(time_left_fraction, 3),
            None if usage_left_fraction is None else round(usage_left_fraction, 3),
            usage_color, account_name, account_name_color,
        )
        if drawing_inputs == self.shown_drawing_inputs:
            return
        self.shown_drawing_inputs = drawing_inputs
        clock_image = draw_mini_clock_image(
            self.size_pixels, time_left_fraction, usage_left_fraction, usage_color, account_name, account_name_color
        )
        # Flatten onto the see-through color (fully transparent pixels become see-through).
        flattened_image = Image.new("RGBA", clock_image.size, self.TRANSPARENT_COLOR)
        flattened_image.alpha_composite(clock_image)
        self.photo_image = ImageTk.PhotoImage(flattened_image.convert("RGB"))
        self.canvas.itemconfigure(self.canvas_image, image=self.photo_image)

    def on_press(self, event):
        self.press_position = (event.x_root, event.y_root)
        self.drag_start_offset = (event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y())
        self.is_dragging = False

    def on_motion(self, event):
        if self.press_position is None:
            return
        if not self.is_dragging and (
            abs(event.x_root - self.press_position[0]) + abs(event.y_root - self.press_position[1]) < 4
        ):
            return  # a small wobble during a click is not a drag
        self.is_dragging = True
        offset_x, offset_y = self.drag_start_offset
        self.window.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")
        self.on_moved()

    def on_release(self, event):
        was_click = self.press_position is not None and not self.is_dragging
        self.press_position = None
        self.is_dragging = False
        if was_click:
            self.on_click()


class FloatingUsageLabel:
    """A small borderless always-on-top text window. Drag it by the grip of dots at its left end; right-click for a
    menu."""

    def __init__(
        self, tk_root, on_quit_requested, on_login_requested, on_account_button_clicked, read_graph_history_entries,
        on_display_mode_changed=None,
    ):
        self.tk_root = tk_root
        self.read_graph_history_entries = read_graph_history_entries
        tk_root.overrideredirect(True)  # no title bar or border
        tk_root.attributes("-topmost", True)
        tk_root.attributes("-alpha", 0.9)
        tk_root.configure(background="#222222")
        # Above the text, two weekly bars in 7 day segments: first (at the very top) the weekly limit left (Claude orange, the
        # partly used day white), then how much SHOULD be left by now, i.e. the time left until the weekly reset
        # (whole days only, in gray; the day in progress and empty parts are not drawn).
        self.weekly_usage_left_bar = SegmentBar(
            tk_root, WEEKLY_PART_COUNT, WEEKLY_LEFT_FULL_DAY_COLOR, WEEKLY_LEFT_PART_DAY_COLOR, gap_below_pixels=1
        )
        self.weekly_expected_left_bar = SegmentBar(
            tk_root, WEEKLY_PART_COUNT, WEEKLY_EXPECTED_LEFT_FULL_DAY_COLOR, part_color=None, show_empty_track=False,
            height_pixels=EXPECTED_BAR_HEIGHT_PIXELS,
        )

        # The text row: "⚡ 47%" in the usage bar's color first, then "⏳ 2h41m" in blue (matching the time bar).
        text_row = tkinter.Frame(tk_root, background="#222222", padx=6, pady=4)
        text_row.pack()
        # At the right end, packed from the right before anything else: a button with the selected account's name, e.g.
        # "DEV" (red when that account is signed out). Clicking it opens the account panel (so it is not a drag
        # handle). It sits in a fixed-size frame as wide as the longest account name, so switching keeps the width.
        self.account_button_frame = tkinter.Frame(text_row, background=ACCOUNT_BUTTON_COLOR, cursor="hand2")
        self.account_button_frame.pack_propagate(False)
        self.account_button_frame.pack(side="right", padx=(4, 0))
        self.account_name_text = tkinter.Label(
            self.account_button_frame,
            text="",
            font=("Segoe UI", 8, "bold"),
            foreground=ACCOUNT_BUTTON_TEXT_COLOR,
            background=ACCOUNT_BUTTON_COLOR,
            cursor="hand2",
        )
        self.account_name_text.pack(expand=True)
        for account_button_part in (self.account_button_frame, self.account_name_text):
            account_button_part.bind("<Button-1>", lambda event: on_account_button_clicked())
            account_button_part.bind("<Enter>", lambda event: self.color_account_button(ACCOUNT_BUTTON_HOVER_COLOR))
            account_button_part.bind("<Leave>", lambda event: self.color_account_button(ACCOUNT_BUTTON_COLOR))
        # At the left end: the drag handle, a dim grip of dots that lights up under the pointer. It is the only place
        # the label can be dragged from (see the bindings below).
        self.drag_handle = tkinter.Label(
            text_row,
            text="⠿",
            font=("Segoe UI Symbol", 11),
            foreground=DRAG_HANDLE_COLOR,
            background=LABEL_BACKGROUND_COLOR,
            cursor="fleur",
            padx=2,
        )
        self.drag_handle.pack(side="left", padx=(0, 4))
        self.drag_handle.bind(
            "<Enter>", lambda event: self.drag_handle.configure(foreground=DRAG_HANDLE_HOVER_COLOR, background="#333333")
        )
        self.drag_handle.bind(
            "<Leave>",
            lambda event: self.drag_handle.configure(foreground=DRAG_HANDLE_COLOR, background=LABEL_BACKGROUND_COLOR),
        )
        label_font = ("Segoe UI", 10, "bold")
        self.usage_percent_text = LeadingZerosGrayText(
            text_row, text="", font=label_font, foreground="#bbbbbb", background="#222222"
        )
        # How much the % left dropped at its last change, e.g. "(03%)", in a lighter, non-bold font.
        self.usage_last_drop_text = LeadingZerosGrayText(
            text_row, text="", font=("Segoe UI", 9), foreground="#bbbbbb", background="#222222"
        )
        self.time_left_text = LeadingZerosGrayText(
            text_row, text=f"⏳{ICON_GAP}loading…", font=label_font, foreground=TIME_LEFT_COLOR, background="#222222"
        )
        # How long the last usage drop took, e.g. "(06m)", lighter and non-bold like the usage drop.
        self.time_of_last_drop_text = LeadingZerosGrayText(
            text_row, text="", font=("Segoe UI", 9), foreground=TIME_LEFT_COLOR, background="#222222"
        )
        # Every text has a fixed-width slot sized for its longest usual value, so the label keeps the same width when
        # "81%" becomes "100%", "3h43m" becomes "?", or the "(3%)" / "(6m)" drops come and go.
        # Each text sits in its own fixed-size box (a frame that doesn't shrink or grow to its content), as wide as the
        # text's longest usual value. (A label's own width counts characters, which rounds up and left big gaps.)
        self.text_slot_frames = {}
        self.text_slot_samples_and_pixels = {}
        for text_label, longest_usual_text, gap_after in (
            (self.usage_percent_text, f"⚡{ICON_GAP}100%", 3),
            (self.usage_last_drop_text, "(99%)", 5),
            (self.time_left_text, f"⏳{ICON_GAP}04h59m", 3),
            (self.time_of_last_drop_text, ("(59m)", "(59s)", "(04h)"), 0),
        ):
            self.put_in_slot(text_label, longest_usual_text, gap_after)
        # Shown only when signed out or the login expired; clicking it opens a "claude auth login" window.
        self.login_button = tkinter.Label(
            text_row,
            text="🔑 Log in",
            font=label_font,
            foreground="white",
            background="#3a6ea5",
            padx=6,
            cursor="hand2",
        )
        self.login_button.bind("<Button-1>", lambda event: on_login_requested())

        # Below the text, two bars in 5 segments (an hour / 20% each): first time left (blue) in the 5-hour window, full
        # at 5h left and empty at 0.
        self.time_left_bar = SegmentBar(tk_root, FIVE_HOUR_PART_COUNT, TIME_LEFT_COLOR, gap_below_pixels=1)
        # Then usage left at the very bottom, full when nothing is used and empty at 100%; green / orange / red by usage.
        self.usage_left_bar = SegmentBar(tk_root, FIVE_HOUR_PART_COUNT, "#bbbbbb")

        self.drag_start_offset = (0, 0)
        self.user_has_moved_label = False
        right_click_menu = tkinter.Menu(tk_root, tearoff=False)
        right_click_menu.add_command(label="Hide label (click the tray icon to bring it back)", command=self.hide)
        right_click_menu.add_command(label="Quit", command=on_quit_requested)
        self.account_name_text.bind(
            "<Button-3>", lambda event: right_click_menu.tk_popup(event.x_root, event.y_root)
        )
        # Only the handle drags the label; right-click anywhere on the text row opens the menu.
        self.drag_handle.bind("<ButtonPress-1>", self.start_drag)
        self.drag_handle.bind("<B1-Motion>", self.continue_drag)
        for right_clickable_widget in (
            self.drag_handle,
            text_row,
            self.time_left_text,
            self.time_of_last_drop_text,
            self.usage_percent_text,
            self.usage_last_drop_text,
            *self.text_slot_frames.values(),
        ):
            right_clickable_widget.bind(
                "<Button-3>", lambda event: right_click_menu.tk_popup(event.x_root, event.y_root)
            )

        # Hovering over the usage (the "⚡ 081% (03%)" text or the usage bar) shows the last 10 hours as a graph.
        self.usage_history_graph = UsageHistoryGraph(tk_root)
        self.graph_hover_widgets = set()
        for hover_widget in (
            self.usage_percent_text,
            self.usage_last_drop_text,
            self.text_slot_frames[self.usage_percent_text],
            self.text_slot_frames[self.usage_last_drop_text],
            self.usage_left_bar.canvas,
        ):
            hover_widget.bind("<Enter>", self.show_usage_history_graph, add="+")
            hover_widget.bind("<Leave>", lambda event: self.tk_root.after(150, self.hide_graph_if_pointer_left), add="+")
            self.graph_hover_widgets.add(hover_widget)
            self.graph_hover_widgets.update(getattr(hover_widget, "part_labels", []))

        # Two display modes: this full label, or the round mini clock. Clicking the time ("⏳ 02h11m (06m)") shrinks
        # to the clock; clicking the clock comes back. Both share one corner: switching keeps the bottom-right edge.
        self.is_minimized = False
        self.is_hidden = False
        self.on_display_mode_changed = on_display_mode_changed
        self.mini_clock = MiniClock(
            tk_root,
            on_click=self.switch_to_full_label,
            on_right_click=lambda event: right_click_menu.tk_popup(event.x_root, event.y_root),
            on_moved=self.note_moved_by_hand,
        )
        self.clock_inputs = {
            "time_left_fraction": None, "usage_left_fraction": None, "usage_color": "#bbbbbb",
            "account_name": "", "account_name_color": ACCOUNT_BUTTON_TEXT_COLOR,
        }
        for time_widget in (self.time_left_text, self.time_of_last_drop_text):
            for clickable_part in (time_widget, self.text_slot_frames[time_widget], *time_widget.part_labels):
                clickable_part.configure(cursor="hand2")
                clickable_part.bind("<Button-1>", lambda event: self.switch_to_mini_clock())

        self.move_to_bottom_right_above_taskbar()
        self.keep_on_top_forever()

    def note_moved_by_hand(self):
        self.user_has_moved_label = True

    def showing_window(self):
        """The window of the current mode: the mini clock's, or the full label's."""
        return self.mini_clock.window if self.is_minimized else self.tk_root

    def switch_to_mini_clock(self):
        self.switch_display_mode(minimized=True)

    def switch_to_full_label(self):
        self.switch_display_mode(minimized=False)

    def switch_display_mode(self, minimized):
        if minimized == self.is_minimized:
            return
        old_window = self.showing_window()
        old_window.update_idletasks()
        old_right = old_window.winfo_rootx() + old_window.winfo_width()
        old_bottom = old_window.winfo_rooty() + old_window.winfo_height()
        self.usage_history_graph.hide()
        self.is_minimized = minimized
        if minimized:
            self.redraw_mini_clock()
        new_window = self.showing_window()
        if not self.is_hidden:
            new_window.deiconify()
            old_window.withdraw()
        new_window.update_idletasks()
        if self.user_has_moved_label:
            # Keep the bottom-right corner where it was.
            new_window.geometry(f"+{old_right - new_window.winfo_width()}+{old_bottom - new_window.winfo_height()}")
        else:
            self.move_to_bottom_right_above_taskbar()
        if self.on_display_mode_changed:
            self.on_display_mode_changed(minimized)

    def redraw_mini_clock(self):
        if self.is_minimized:
            self.mini_clock.draw(**self.clock_inputs)

    def show_usage_history_graph(self, event=None):
        if not self.usage_history_graph.is_showing():
            self.usage_history_graph.show_above(self.tk_root, self.read_graph_history_entries())
            self.tk_root.after(200, self.keep_graph_while_pointer_stays)

    def keep_graph_while_pointer_stays(self):
        """Tk doesn't always get a Leave event (e.g. when the pointer jumps away), so also check 5 times a second."""
        if self.usage_history_graph.is_showing():
            self.hide_graph_if_pointer_left()
            self.tk_root.after(200, self.keep_graph_while_pointer_stays)

    def hide_graph_if_pointer_left(self):
        """Moving between the parts of the usage text fires Leave then Enter, so only hide once the pointer is really
        off all of them."""
        try:
            pointer_widget = self.tk_root.winfo_containing(*self.tk_root.winfo_pointerxy())
        except (KeyError, tkinter.TclError):  # over a menu or other window Tk can't name
            pointer_widget = None
        if pointer_widget not in self.graph_hover_widgets:
            self.usage_history_graph.hide()

    def move_to_bottom_right_above_taskbar(self):
        """Place it at the bottom-right of the main monitor's work area (the screen minus the taskbar).

        Uses the real window and real screen positions from Windows rather than tkinter's geometry, because with two
        monitors at different scaling tkinter's placement sometimes landed on the second monitor."""
        showing_window = self.showing_window()
        showing_window.update_idletasks()
        window_handle = ctypes.windll.user32.GetAncestor(showing_window.winfo_id(), GA_ROOT)
        window_rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(window_handle, ctypes.byref(window_rect))
        work_area = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work_area), 0)

        window_width = window_rect.right - window_rect.left
        window_height = window_rect.bottom - window_rect.top
        gap_pixels = 8
        target_left = work_area.right - window_width - gap_pixels
        target_top = work_area.bottom - window_height - gap_pixels
        if (window_rect.left, window_rect.top) != (target_left, target_top):
            # Record each correction, to find out why the label sometimes lands on the second monitor.
            with open(PLACEMENT_LOG_FILE_PATH, "a", encoding="utf-8") as placement_log:
                placement_log.write(
                    f"{datetime.now():%Y-%m-%d %H:%M:%S} moving label from ({window_rect.left},{window_rect.top}) "
                    f"to ({target_left},{target_top}); work area right/bottom = {work_area.right}/{work_area.bottom}\n"
                )
            ctypes.windll.user32.SetWindowPos(
                window_handle, None, target_left, target_top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            )

    def start_drag(self, event):
        self.drag_start_offset = (event.x_root - self.tk_root.winfo_x(), event.y_root - self.tk_root.winfo_y())

    def continue_drag(self, event):
        self.user_has_moved_label = True
        offset_x, offset_y = self.drag_start_offset
        self.tk_root.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def show(
        self,
        time_text,
        usage_color,
        usage_percent_text="",
        usage_last_drop_text="",
        time_of_last_drop_text="",
        usage_left_fraction=None,
        time_left_fraction=None,
        show_login_button=False,
        weekly_percent_used=None,
        minutes_left_in_week=None,
    ):
        """Fractions are 0..1; None (unknown) draws an empty track rather than a guess."""
        self.weekly_usage_left_bar.show_fraction(None if weekly_percent_used is None else 1 - weekly_percent_used / 100)
        self.weekly_expected_left_bar.show_fraction(
            None if minutes_left_in_week is None else minutes_left_in_week / (7 * 24 * 60)
        )
        self.set_slot_text(self.time_left_text, time_text)
        self.set_slot_text(self.usage_percent_text, usage_percent_text, foreground=usage_color)
        # No drop yet (but numbers showing): all-gray "(00%)" / "(00s)" placeholders, so the spots are never blank.
        showing_numbers = bool(usage_percent_text)
        for drop_text_label, drop_text, drop_color, placeholder_text in (
            (self.usage_last_drop_text, usage_last_drop_text, usage_color, "(00%)"),
            (self.time_of_last_drop_text, time_of_last_drop_text, TIME_LEFT_COLOR, "(00s)"),
        ):
            if not drop_text and showing_numbers:
                self.set_slot_text(drop_text_label, placeholder_text, foreground=LEADING_ZEROS_COLOR)
            else:
                self.set_slot_text(drop_text_label, drop_text, foreground=drop_color)
        self.login_button.pack_forget()
        if show_login_button:
            self.login_button.pack(side="left", padx=(8, 0))
        self.usage_left_bar.show_fraction(usage_left_fraction, usage_color)
        self.time_left_bar.show_fraction(time_left_fraction)
        self.clock_inputs.update(
            time_left_fraction=time_left_fraction, usage_left_fraction=usage_left_fraction, usage_color=usage_color
        )
        self.redraw_mini_clock()
        if not self.user_has_moved_label:
            # The text width changes, so keep the right edge pinned to the screen corner.
            self.move_to_bottom_right_above_taskbar()

    def put_in_slot(self, text_label, longest_usual_texts, gap_after):
        """Pack text_label inside a new fixed-size frame in the text row, sized to how big the widest of
        longest_usual_texts (one text, or a tuple of candidates) draws."""
        slot_frame = tkinter.Frame(text_label.master, background="#222222")
        slot_frame.pack_propagate(False)  # keep the frame's own size whatever the text inside is
        slot_frame.pack(side="left", padx=(0, gap_after))
        text_label.pack(in_=slot_frame, side="left")
        text_label.lift(slot_frame)  # the label was made before the frame, so it would be drawn underneath
        self.text_slot_frames[text_label] = slot_frame
        if isinstance(longest_usual_texts, str):
            longest_usual_texts = (longest_usual_texts,)
        label_font_measurer = tkinter.font.Font(font=text_label.cget("font"))
        self.measure_slot(text_label, max(longest_usual_texts, key=label_font_measurer.measure))

    def measure_slot(self, text_label, longest_usual_text):
        current_text = text_label.cget("text")
        text_label.configure(text=longest_usual_text)
        text_label.update_idletasks()
        self.text_slot_samples_and_pixels[text_label] = (
            longest_usual_text, text_label.winfo_reqwidth(), text_label.winfo_reqheight()
        )
        self.set_slot_text(text_label, current_text)

    def set_slot_text(self, text_label, text, **other_options):
        """Show text in its fixed-size slot; a longer text (an error message) widens the slot rather than being cut."""
        longest_usual_text, slot_width, slot_height = self.text_slot_samples_and_pixels[text_label]
        text_label.configure(text=text, **other_options)
        text_label.update_idletasks()
        self.text_slot_frames[text_label].configure(
            width=max(slot_width, text_label.winfo_reqwidth()), height=slot_height
        )

    def show_account_name(self, account_name, all_account_names, is_logged_in):
        """The account button: the name in light gray, or red when that account is signed out. Its frame fits the
        longest account name, so switching accounts doesn't change the label's width ("fin" and "dev" are not
        equally wide)."""
        name_font_measurer = tkinter.font.Font(font=self.account_name_text.cget("font"))
        longest_name_pixels = max(name_font_measurer.measure(name.upper()) for name in all_account_names)
        self.account_name_text.configure(
            text=account_name.upper(),  # "FIN" / "DEV"
            foreground=ACCOUNT_BUTTON_TEXT_COLOR if is_logged_in else ACCOUNT_LOGGED_OUT_TEXT_COLOR,
        )
        self.account_name_text.update_idletasks()
        self.account_button_frame.configure(
            width=longest_name_pixels + 12, height=self.account_name_text.winfo_reqheight() + 2
        )
        self.clock_inputs.update(
            account_name=account_name,
            account_name_color=ACCOUNT_BUTTON_TEXT_COLOR if is_logged_in else ACCOUNT_LOGGED_OUT_TEXT_COLOR,
        )
        self.redraw_mini_clock()

    def color_account_button(self, background_color):
        self.account_button_frame.configure(background=background_color)
        self.account_name_text.configure(background=background_color)

    def hide(self):
        self.is_hidden = True
        self.usage_history_graph.hide()
        self.tk_root.withdraw()
        self.mini_clock.window.withdraw()

    def show_at_default_position(self):
        """From the tray icon: show the current mode (full label or mini clock) back in its corner."""
        self.is_hidden = False
        self.showing_window().deiconify()
        self.user_has_moved_label = False
        self.move_to_bottom_right_above_taskbar()

    def keep_on_top_forever(self):
        """Re-applied 4 times a second: Windows can push it down (a taskbar click puts the taskbar above it), and can
        move it (to the second monitor), so it is raised and, unless dragged by hand, put back in its corner."""
        showing_window = self.showing_window()
        if showing_window.state() != "withdrawn":
            showing_window.attributes("-topmost", False)
            showing_window.attributes("-topmost", True)
            showing_window.lift()
            if not self.user_has_moved_label:
                self.move_to_bottom_right_above_taskbar()
        self.tk_root.after(STAY_ON_TOP_CHECK_MILLISECONDS, self.keep_on_top_forever)


class UsageTrayApp:
    def __init__(self):
        # Warnings already shown, keyed by the 5-hour window's reset time, so each shows once per window.
        try:
            self.already_shown_warnings = set(json.loads(SHOWN_WARNINGS_FILE_PATH.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            self.already_shown_warnings = set()
        self.stop_event = threading.Event()
        # Set from tray-menu clicks (another thread); acted on by the tkinter loop, which owns the label.
        self.refresh_requested_event = threading.Event()
        self.show_label_requested_event = threading.Event()
        self.last_fetch_error_text = None
        # True while the last fetch failed because of the login; shows the "Log in" button and tray menu item.
        self.login_is_needed = False
        self.claude_login_window_process = None
        self.fetch_now_event = threading.Event()
        # Which Claude account the display follows (picked in the tray menu → Account).
        self.selected_account = self.find_account(read_tray_settings().get("selectedAccountName"))
        self.account_changed_event = threading.Event()
        self.add_account_requested_event = threading.Event()
        # For the "(3%)" after the % left: what was shown last, and how much it dropped at its last change.
        self.previously_shown_percent_left = None
        self.last_drop_in_percent_left = None
        # For the "(6m)" after the time left: when the % left last changed, and how long the last drop took.
        self.time_of_previous_change = None
        self.minutes_taken_by_last_drop = None
        self.tk_root = tkinter.Tk()
        self.floating_label = FloatingUsageLabel(
            self.tk_root,
            on_quit_requested=self.quit,
            on_login_requested=self.open_claude_login_window,
            on_account_button_clicked=self.toggle_account_panel,
            read_graph_history_entries=self.read_selected_account_history,
            on_display_mode_changed=self.save_display_mode,
        )
        # Start in the mode it was left in (full label or mini clock).
        if read_tray_settings().get("displayMode") == "mini":
            self.floating_label.switch_to_mini_clock()
        self.account_panel = AccountPanel(
            self.tk_root,
            on_select_account=self.select_account_from_panel,
            on_login=lambda account_name: self.open_claude_auth_window(self.find_account(account_name), "login"),
            on_logout=lambda account_name: self.open_claude_auth_window(self.find_account(account_name), "logout"),
            on_renew=lambda account_name: self.renew_login(self.find_account(account_name)),
        )
        self.shown_account_button_state = None
        self.tray_icon = pystray.Icon(
            "claude-usage",
            icon=draw_icon_image(None),
            title="Claude usage: waiting for data…",
            menu=pystray.Menu(
                # default=True: this also runs when the tray icon is left-clicked.
                pystray.MenuItem(
                    "Show label at default position", lambda: self.show_label_requested_event.set(), default=True
                ),
                pystray.MenuItem(
                    "Log in to Claude…",
                    lambda: self.open_claude_login_window(),
                    visible=lambda menu_item: self.login_is_needed,
                ),
                pystray.MenuItem("Account", pystray.Menu(self.build_account_menu_items)),
                pystray.MenuItem("Refresh now", lambda: self.fetch_now_event.set()),
                pystray.MenuItem("Quit", lambda: self.quit()),
            ),
        )
        self.account_changed_event.set()  # show the account name on the label once the loop starts

    @staticmethod
    def save_display_mode(minimized):
        tray_settings = read_tray_settings()
        tray_settings["displayMode"] = "mini" if minimized else "full"
        save_tray_settings(tray_settings)

    def read_selected_account_history(self):
        return [
            history_entry for history_entry in read_usage_history()
            if Path(history_entry.get("accountFolder") or DEFAULT_CLAUDE_CONFIG_FOLDER)
            == Path(self.selected_account["folder"])
        ]

    @staticmethod
    def find_account(account_name):
        """The account with this name, or the default one if there is none (e.g. it was removed from the settings)."""
        all_accounts = list_claude_accounts()
        return next((account for account in all_accounts if account["name"] == account_name), all_accounts[0])

    def build_account_menu_items(self):
        """Tray menu → Account: one choice per account (re-read each time the menu opens), then "Add …"."""
        def make_account_menu_item(account):
            # A function per account, because pystray refuses click handlers with extra (default) arguments.
            account_email = read_account_email(account["folder"]) or "not signed in"
            return pystray.MenuItem(
                f"{account['name']}  ({account_email})",
                lambda: self.select_account(account),
                checked=lambda menu_item: account["name"] == self.selected_account["name"],
                radio=True,
            )

        for account in list_claude_accounts():
            yield make_account_menu_item(account)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Add another account…", lambda: self.add_account_requested_event.set())

    def select_account(self, account):
        """Runs on the tray thread; the label changes on the tkinter loop (account_changed_event)."""
        self.selected_account = account
        tray_settings = read_tray_settings()
        tray_settings["selectedAccountName"] = account["name"]
        save_tray_settings(tray_settings)
        self.last_fetch_error_text = None
        self.login_is_needed = False
        self.account_changed_event.set()
        self.fetch_now_event.set()


    def add_account(self):
        """Runs on the tkinter loop: ask for a short name, make a separate Claude settings folder for it, select it,
        and open the login window for it. You sign in yourself in the browser."""
        account_name = tkinter.simpledialog.askstring(
            "Add Claude account",
            "Short name for the new account (letters, digits, - or _), e.g. work2:",
            parent=self.tk_root,
        )
        if not account_name:
            return
        account_name = account_name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", account_name) or any(
            account["name"] == account_name for account in list_claude_accounts()
        ):
            self.tray_icon.notify(f'"{account_name}" can not be used: pick a new short name.', "Claude usage")
            return
        new_account = {"name": account_name, "folder": str(Path.home() / f".claude-{account_name}")}
        Path(new_account["folder"]).mkdir(exist_ok=True)
        tray_settings = read_tray_settings()
        tray_settings.setdefault("extraAccounts", []).append(new_account)
        save_tray_settings(tray_settings)
        self.select_account(new_account)
        self.open_claude_login_window()

    def refresh(self):
        usage_snapshot = read_usage_snapshot()
        now_utc = datetime.now(timezone.utc)
        # Right after switching accounts the saved numbers are still the other account's (files from before accounts
        # existed belong to the default one); wait for the fetch instead.
        if usage_snapshot is not None and Path(
            usage_snapshot.get("accountFolder", DEFAULT_CLAUDE_CONFIG_FOLDER)
        ) != Path(self.selected_account["folder"]):
            usage_snapshot = None

        if usage_snapshot is None:
            self.tray_icon.icon = draw_icon_image(None)
            reason_text = self.last_fetch_error_text or "loading…"
            self.tray_icon.title = f"Claude usage: {reason_text}"[:127]
            self.floating_label.show(f"⏳{ICON_GAP}{reason_text}", "#bbbbbb", show_login_button=self.login_is_needed)
            return

        five_hour = usage_snapshot.get("fiveHour") or {}
        weekly = usage_snapshot.get("weekly") or {}
        five_hour_percent = five_hour.get("percentUsed")
        minutes_left_in_five_hour_window = minutes_until(five_hour.get("resetsAt"), now_utc)
        minutes_left_in_weekly_window = minutes_until(weekly.get("resetsAt"), now_utc)
        minutes_since_update = minutes_until(now_utc.isoformat(), datetime.fromisoformat(usage_snapshot["updatedAt"]))

        # Once a window's reset time has passed, its saved % is meaningless: the next window starts with your next
        # message to Claude (the next fetch picks it up).
        five_hour_window_has_reset = minutes_left_in_five_hour_window == 0
        weekly_window_has_reset = minutes_left_in_weekly_window == 0
        if five_hour_window_has_reset:
            five_hour_percent = None

        hover_text_lines = []
        if len(list_claude_accounts()) > 1:
            hover_text_lines.append(
                f"{self.selected_account['name']}: {read_account_email(self.selected_account['folder']) or '?'}"
            )
        if five_hour_window_has_reset:
            hover_text_lines.append("5h: reset (new window starts on your next message)")
        else:
            if minutes_left_in_five_hour_window is not None:
                hover_text_lines.append(
                    f"⏳ {format_minutes_as_hours_and_minutes(minutes_left_in_five_hour_window)} left"
                )
            if five_hour_percent is not None:
                hover_text_lines.append(f"5h: {five_hour_percent:.0f}%")
        if weekly_window_has_reset:
            hover_text_lines.append("Week: reset")
        elif weekly.get("percentUsed") is not None:
            hover_text_lines.append(f"Week: {weekly['percentUsed']:.0f}%")
        if self.last_fetch_error_text:
            hover_text_lines.append(f"Can't update: {self.last_fetch_error_text}")
        hover_text_lines.append(f"As of {format_minutes_as_hours_and_minutes(minutes_since_update or 0)} ago")

        # Old numbers that can't be refreshed are misleading (e.g. "100% reset" two days later), so show the problem
        # on the label instead, in gray with empty bars.
        if self.last_fetch_error_text and (minutes_since_update or 0) > SHOW_ERROR_ON_LABEL_AFTER_MINUTES:
            self.tray_icon.icon = draw_icon_image(None)
            self.tray_icon.title = "\n".join(hover_text_lines)[:127]
            self.floating_label.show(
                f"⚠ {self.last_fetch_error_text}", "#bbbbbb", show_login_button=self.login_is_needed
            )
            return

        self.tray_icon.icon = draw_icon_image(five_hour_percent)
        self.tray_icon.title = "\n".join(hover_text_lines)[:127]  # Windows limits hover text to 127 characters

        # Label text: the 5-hour usage LEFT as "⚡ 053%", then "⏳ 02h41m" in blue (fixed length, leading zeros gray).
        if five_hour_window_has_reset:
            time_text = f"⏳{ICON_GAP}reset"
        elif minutes_left_in_five_hour_window is None:
            time_text = f"⏳{ICON_GAP}?"
        else:
            time_text = f"⏳{ICON_GAP}{format_minutes_fixed_length(minutes_left_in_five_hour_window)}"
        # After a reset nothing of the new window is used yet, so it shows as 100% left with a full usage bar.
        label_percent_used = 0 if five_hour_window_has_reset else five_hour_percent
        usage_last_drop_text = ""
        time_of_last_drop_text = ""
        if label_percent_used is None:
            usage_percent_text = ""
            usage_left_fraction = None
        else:
            # Shows what is LEFT, like the usage bar: 100% at the start of a window, 000% when it is used up.
            usage_percent_left = max(0, round(100 - label_percent_used))
            usage_percent_text = f"⚡{ICON_GAP}{usage_percent_left:03d}%"
            usage_left_fraction = (100 - label_percent_used) / 100
            # Only fresh numbers count towards the drop: at start-up the saved file can be days old, and its stale
            # value would otherwise show up as a bogus drop once the first fresh fetch arrives.
            numbers_are_fresh = (minutes_since_update or 0) <= SHOW_ERROR_ON_LABEL_AFTER_MINUTES
            if numbers_are_fresh:
                usage_last_drop_text, time_of_last_drop_text = self.track_last_drop_in_percent_left(
                    usage_percent_left, now_utc
                )
        if minutes_left_in_five_hour_window is None:
            time_left_fraction = None
        else:
            time_left_fraction = minutes_left_in_five_hour_window / FIVE_HOUR_WINDOW_MINUTES  # 0 left -> empty
        self.floating_label.show(
            time_text,
            pick_usage_bar_color_for_percent(label_percent_used),
            usage_percent_text=usage_percent_text,
            usage_last_drop_text=usage_last_drop_text,
            time_of_last_drop_text=time_of_last_drop_text,
            usage_left_fraction=usage_left_fraction,
            time_left_fraction=time_left_fraction,
            show_login_button=self.login_is_needed,
            # After the weekly reset nothing of the new week is used yet: a full bar.
            weekly_percent_used=0 if weekly_window_has_reset else weekly.get("percentUsed"),
            # After the reset a whole new week is ahead, until the next fetch brings its real reset time.
            minutes_left_in_week=7 * 24 * 60 if weekly_window_has_reset else minutes_left_in_weekly_window,
        )

        if not five_hour_window_has_reset:
            self.show_warnings_if_needed(five_hour, five_hour_percent, minutes_left_in_five_hour_window)

    def track_last_drop_in_percent_left(self, usage_percent_left, now_utc):
        """Return ("(03%)", "(06m)") when the % left last went down by 3 (e.g. 86% -> 83%) over 6 minutes since the
        change before it. Both stay until the next change, since most fetches find no change, and both clear when the
        % left goes up (a new 5-hour window). The time is left out for the first drop after the app starts, because
        the app doesn't know when the change before it happened."""
        previous_percent_left = self.previously_shown_percent_left
        if previous_percent_left is not None and usage_percent_left != previous_percent_left:
            if usage_percent_left < previous_percent_left:
                self.last_drop_in_percent_left = previous_percent_left - usage_percent_left
                if self.time_of_previous_change is None:
                    self.minutes_taken_by_last_drop = None
                else:
                    self.minutes_taken_by_last_drop = (now_utc - self.time_of_previous_change).total_seconds() / 60
            else:
                self.last_drop_in_percent_left = None
                self.minutes_taken_by_last_drop = None
            self.time_of_previous_change = now_utc
        self.previously_shown_percent_left = usage_percent_left

        if self.last_drop_in_percent_left is None:
            usage_last_drop_text = ""
        else:
            usage_last_drop_text = f"({min(99, self.last_drop_in_percent_left):02d}%)"
        if self.minutes_taken_by_last_drop is None:
            time_of_last_drop_text = ""
        else:
            time_of_last_drop_text = f"({format_duration_fixed_length(self.minutes_taken_by_last_drop)})"
        return usage_last_drop_text, time_of_last_drop_text

    def show_warnings_if_needed(self, five_hour, five_hour_percent, minutes_left_in_five_hour_window):
        """Two notifications, each once per 5-hour window: usage crossing 70%, and the last 30 minutes."""
        if not five_hour.get("resetsAt"):
            return
        # The server's reset time wobbles by a fraction of a second between fetches (16:09:59.7 vs 16:10:00.1), so
        # round it to the minute to recognise the same window every time.
        reset_time_utc = datetime.fromisoformat(five_hour["resetsAt"].replace("Z", "+00:00"))
        window_key = (reset_time_utc + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M")

        if five_hour_percent is not None and five_hour_percent >= HIGH_USAGE_WARNING_PERCENTAGE:
            self.notify_once(
                f"{window_key}:{HIGH_USAGE_WARNING_PERCENTAGE}%",
                f"You've used {five_hour_percent:.0f}% of your 5-hour limit "
                f"({100 - five_hour_percent:.0f}% left, resets in "
                f"{format_minutes_as_hours_and_minutes(minutes_left_in_five_hour_window or 0)}).",
            )

        if (
            minutes_left_in_five_hour_window is not None
            and minutes_left_in_five_hour_window <= WARN_WHEN_MINUTES_UNTIL_RESET_AT_OR_BELOW
        ):
            percent_left_text = "" if five_hour_percent is None else f" ({100 - five_hour_percent:.0f}% left)"
            self.notify_once(
                f"{window_key}:last-30-minutes",
                f"5-hour window resets in {minutes_left_in_five_hour_window} min{percent_left_text}.",
            )

    def notify_once(self, warning_key, message):
        """Show a notification only if it was never shown before, even across app restarts."""
        if warning_key in self.already_shown_warnings:
            return
        self.already_shown_warnings.add(warning_key)
        try:
            # Keep only the most recent keys, so the file stays tiny.
            SHOWN_WARNINGS_FILE_PATH.write_text(json.dumps(sorted(self.already_shown_warnings)[-50:]), encoding="utf-8")
        except OSError:
            pass  # not being able to remember is no reason to skip the notification
        self.tray_icon.notify(message, "Claude usage")

    def read_login_state(self, account):
        """"in": a login token that hasn't expired yet; "expired": the token (which lasts hours) ran out, or the
        server turned it down, but the saved login can still renew it; "out": signed out. Only the expiry times are
        read: the tokens themselves are never read out or shown."""
        try:
            claude_login = json.loads((Path(account["folder"]) / ".credentials.json").read_text(encoding="utf-8"))[
                "claudeAiOauth"
            ]
        except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
            return "out"
        now_milliseconds = datetime.now(timezone.utc).timestamp() * 1000
        can_renew = bool(claude_login.get("refreshToken")) and (
            claude_login.get("refreshTokenExpiresAt") or float("inf")
        ) > now_milliseconds
        token_is_valid = bool(claude_login.get("accessToken")) and (
            claude_login.get("expiresAt") or float("inf")
        ) > now_milliseconds
        if account["name"] == self.selected_account["name"] and self.login_is_needed:
            token_is_valid = False  # the server said "login expired"
        if token_is_valid:
            return "in"
        return "expired" if can_renew else "out"

    def renew_login(self, account):
        """Run "claude auth status" for that account, hidden: Claude Code then renews the expired token from the
        saved login (it writes the new one to the credentials file itself). Then fetch again."""
        renew_environment = dict(os.environ)
        renew_environment.pop("CLAUDE_CONFIG_DIR", None)
        if Path(account["folder"]) != DEFAULT_CLAUDE_CONFIG_FOLDER:
            renew_environment["CLAUDE_CONFIG_DIR"] = account["folder"]

        def renew_then_fetch():
            try:
                subprocess.run(
                    ["cmd.exe", "/c", "claude auth status"], env=renew_environment, capture_output=True,
                    timeout=60, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            if account["name"] == self.selected_account["name"]:
                self.login_is_needed = False
                self.fetch_now_event.set()

        threading.Thread(target=renew_then_fetch, daemon=True).start()

    @staticmethod
    def describe_five_hour_window(latest_history_entry, now_utc):
        """(text, color) for an account's row in the panel, from its last saved fetch: the time left in its 5-hour
        window, "8m left" / "1h 25m left" (right-aligned), or "n/a" when there's no window running (none fetched yet, or it reset)."""
        minutes_left = None
        if latest_history_entry is not None:
            minutes_left = minutes_until(latest_history_entry.get("fiveHourResetsAt"), now_utc)
        if not minutes_left:
            return "n/a", "#777777"
        hours, minutes = divmod(minutes_left, 60)
        return (f"{hours}h {minutes}m left" if hours else f"{minutes}m left"), TIME_LEFT_COLOR

    @staticmethod
    def find_suspicious_account_pairs(accounts_with_latest_entries):
        """Two different Claude accounts never have the same 5-hour % used AND the same reset time (to the minute), or
        the same email: if they do, the app most likely fetched one account's numbers with the other one's login.
        Returns alert texts, e.g. "FIN and DEV show the same usage and reset time"."""
        alert_texts = []
        for first_position, (first_account, first_entry, first_email) in enumerate(accounts_with_latest_entries):
            for second_account, second_entry, second_email in accounts_with_latest_entries[first_position + 1:]:
                both_names = f"{first_account['name'].upper()} and {second_account['name'].upper()}"
                if first_email and first_email == second_email:
                    alert_texts.append(f"{both_names} are signed in to the same email")
                if first_entry is None or second_entry is None:
                    continue
                if first_entry.get("fiveHourPercentUsed") is None or not first_entry.get("fiveHourResetsAt"):
                    continue
                first_reset_utc = datetime.fromisoformat(first_entry["fiveHourResetsAt"].replace("Z", "+00:00"))
                second_reset_text = second_entry.get("fiveHourResetsAt")
                if not second_reset_text:
                    continue
                second_reset_utc = datetime.fromisoformat(second_reset_text.replace("Z", "+00:00"))
                if (
                    first_entry["fiveHourPercentUsed"] == second_entry.get("fiveHourPercentUsed")
                    and abs((first_reset_utc - second_reset_utc).total_seconds()) < 60
                ):
                    alert_texts.append(f"{both_names} show the same usage and reset time")
        return alert_texts

    def build_account_panel_data(self):
        """(account rows, alert texts) for the account panel."""
        now_utc = datetime.now(timezone.utc)
        all_history_entries = read_usage_history()
        account_rows = []
        accounts_with_latest_entries = []
        for account in list_claude_accounts():
            latest_history_entry = next(
                (
                    history_entry for history_entry in reversed(all_history_entries)
                    if Path(history_entry.get("accountFolder") or DEFAULT_CLAUDE_CONFIG_FOLDER) == Path(account["folder"])
                ),
                None,
            )
            usage_text, usage_color = self.describe_five_hour_window(latest_history_entry, now_utc)
            account_email = read_account_email(account["folder"])
            accounts_with_latest_entries.append((account, latest_history_entry, account_email))
            account_rows.append({
                "name": account["name"],
                "email": account_email,
                "loginState": self.read_login_state(account),
                "isSelected": account["name"] == self.selected_account["name"],
                "usageText": usage_text,
                "usageColor": usage_color,
            })
        return account_rows, self.find_suspicious_account_pairs(accounts_with_latest_entries)

    def select_account_from_panel(self, account_name):
        self.select_account(self.find_account(account_name))
        self.account_panel.hide()

    def toggle_account_panel(self):
        if self.account_panel.is_showing():
            self.account_panel.hide()
        else:
            self.account_panel.show_above(self.tk_root, *self.build_account_panel_data())

    def update_account_button_and_panel(self):
        """Every second: the account button turns red / back when the selected account signs out / in, and the open
        panel follows logins and logouts finishing in their console windows."""
        account_names = [account["name"] for account in list_claude_accounts()]
        button_state = (
            self.selected_account["name"], tuple(account_names),
            self.read_login_state(self.selected_account) == "in",
        )
        if button_state != self.shown_account_button_state:
            self.shown_account_button_state = button_state
            self.floating_label.show_account_name(self.selected_account["name"], account_names, button_state[2])
        if self.account_panel.is_showing():
            self.account_panel.update_if_changed(*self.build_account_panel_data())

    def open_claude_login_window(self):
        self.open_claude_auth_window(self.selected_account, "login")

    def open_claude_auth_window(self, account, auth_action):
        """Open a console window running "claude auth login" (it opens the browser to sign in) or "claude auth
        logout" for that account, then fetch again once that window closes. You sign in yourself; the app never sees
        your password."""
        if self.claude_login_window_process and self.claude_login_window_process.poll() is None:
            return  # already open
        # The account's folder: CLAUDE_CONFIG_DIR makes Claude Code keep that login there.
        login_environment = dict(os.environ)
        login_environment.pop("CLAUDE_CONFIG_DIR", None)
        if Path(account["folder"]) != DEFAULT_CLAUDE_CONFIG_FOLDER:
            login_environment["CLAUDE_CONFIG_DIR"] = account["folder"]
        seconds_to_keep_window = 10 if auth_action == "login" else 3
        self.claude_login_window_process = subprocess.Popen(
            [
                "cmd.exe",
                "/c",
                f"title Claude {auth_action}: {account['name']} & claude auth {auth_action}"
                f" & timeout /t {seconds_to_keep_window}",
            ],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            env=login_environment,
        )

        def fetch_again_when_login_window_closes(login_window_process):
            login_window_process.wait()
            self.fetch_now_event.set()

        threading.Thread(
            target=fetch_again_when_login_window_closes, args=(self.claude_login_window_process,), daemon=True
        ).start()

    def quit(self):
        self.stop_event.set()
        self.fetch_now_event.set()

    def check_requests_every_second(self, seconds_since_last_refresh):
        """Runs on the tkinter loop: handles tray-menu requests and refreshes every REFRESH_INTERVAL_SECONDS."""
        if self.stop_event.is_set():
            self.tray_icon.stop()
            self.tk_root.destroy()
            return
        if self.refresh_requested_event.is_set() or seconds_since_last_refresh >= REFRESH_INTERVAL_SECONDS:
            self.refresh_requested_event.clear()
            self.refresh()
            seconds_since_last_refresh = 0
        self.update_account_button_and_panel()
        self.tk_root.after(1000, self.check_requests_every_second, seconds_since_last_refresh + 1)

    def watch_for_tray_icon_clicks(self):
        """Runs on the tkinter loop 5 times a second, so clicking the tray icon moves the label back right away."""
        if self.show_label_requested_event.is_set():
            self.show_label_requested_event.clear()
            self.floating_label.show_at_default_position()
        if self.account_changed_event.is_set():
            self.account_changed_event.clear()
            self.shown_account_button_state = None  # redraw the account button
            self.update_account_button_and_panel()
            # The "(3%)" / "(6m)" drops belong to the previous account.
            self.previously_shown_percent_left = None
            self.last_drop_in_percent_left = None
            self.time_of_previous_change = None
            self.minutes_taken_by_last_drop = None
            self.refresh_requested_event.set()
        if self.add_account_requested_event.is_set():
            self.add_account_requested_event.clear()
            self.add_account()
        if not self.stop_event.is_set():
            self.tk_root.after(200, self.watch_for_tray_icon_clicks)

    def fetch_live_usage_forever(self):
        """Background thread: fetch live usage every FETCH_INTERVAL_SECONDS and save it for the display to read."""
        while not self.stop_event.is_set():
            seconds_until_next_fetch = FETCH_INTERVAL_SECONDS
            try:
                usage_snapshot = fetch_live_usage_snapshot(self.selected_account["folder"])
                save_usage_snapshot(usage_snapshot)
                try:
                    append_usage_history(usage_snapshot)
                except OSError:
                    pass  # the graph missing one point is no reason to report a failed fetch
                self.last_fetch_error_text = None
                self.login_is_needed = False
            except UsageLoginNeededError as login_error:
                self.last_fetch_error_text = str(login_error)
                self.login_is_needed = True
            except UsageServerBusyError as busy_error:
                # Asking again before the server's wait is over only keeps the block going.
                self.last_fetch_error_text = str(busy_error)
                seconds_until_next_fetch = busy_error.seconds_to_wait
            except UsageFetchError as fetch_error:
                self.last_fetch_error_text = str(fetch_error)
            except Exception as unexpected_error:  # keep the thread alive whatever happens
                self.last_fetch_error_text = f"unexpected error: {unexpected_error}"
            try:
                self.tray_icon.update_menu()  # show or hide "Log in to Claude…"
            except Exception:
                pass  # the tray icon may not be up yet on the first fetch; it reads the menu fresh when opened
            self.refresh_requested_event.set()
            # Wake early when "Refresh now" or Quit is clicked.
            self.fetch_now_event.wait(seconds_until_next_fetch)
            self.fetch_now_event.clear()

    def run(self):
        threading.Thread(target=self.fetch_live_usage_forever, daemon=True).start()
        # The tray icon runs on its own thread; tkinter must own the main thread.
        self.tray_icon.run_detached()
        self.tray_icon.visible = True
        self.check_requests_every_second(seconds_since_last_refresh=REFRESH_INTERVAL_SECONDS)
        self.watch_for_tray_icon_clicks()
        self.tk_root.mainloop()


def another_copy_is_already_running():
    """Use a named Windows mutex so double-clicking the shortcut twice doesn't make two tray icons."""
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    global single_instance_mutex_handle  # keep the handle alive for as long as the app runs
    single_instance_mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "ClaudeUsageTrayApp")
    return ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    import ctypes

    # "System" DPI awareness: coordinates are in the main monitor's real pixels. (Per-monitor awareness made
    # tkinter land on the second monitor, which has a different scaling.)
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    if not another_copy_is_already_running():
        UsageTrayApp().run()
