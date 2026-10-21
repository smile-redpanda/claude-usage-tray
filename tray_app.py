"""Windows taskbar (system tray) app that shows your Claude plan usage.

Every 5 minutes it fetches your live usage from Anthropic (the same undocumented address Claude Code's /usage screen
uses, signed in with your Claude Code login), saves it to USAGE_JSON_FILE_PATH, and shows:
  - a small floating label (always on top, drag it anywhere): "⚡ 053% (03%)  ⏳ 03h21m (06m)" (% of the 5-hour limit
    left and its last drop, in the usage color; time in blue), the weekly limit as 7 circles, and the account name;
    a blue time-left bar on top and a green / orange / red usage-left bar below
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
import math
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
from PIL import Image, ImageDraw, ImageFont, ImageTk

USAGE_JSON_FILE_PATH = Path.home() / ".claude" / "usage-watch.json"
SHOWN_WARNINGS_FILE_PATH = Path.home() / ".claude" / "usage-tray-shown-warnings.json"
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
TIME_LEFT_COLOR = "#5aaaff"  # blue: the time-left bar and the label text
# A thin space between "⚡" / "⏳" and the number; a normal space looked too far apart.
ICON_GAP = "\u2009"
# Numbers on the label have a fixed length ("081%", "03h43m", "(06m)"), so its width never changes; the leading zeros
# are drawn in this dim gray so "081%" still reads as 81%.
LEADING_ZEROS_COLOR = "#5a5a5a"
# The weekly limit as 7 circles, one per day's share.
WEEKLY_PART_COUNT = 7
WEEKLY_CIRCLE_GRID_SIZE = 3  # 3 rows x 3 columns
WEEKLY_CIRCLE_DIAMETER_PIXELS = 7
WEEKLY_CIRCLE_DRAWING_SCALE = 4  # drawn 4x bigger, then shrunk, for smooth edges
WEEKLY_CIRCLE_GAP_PIXELS = 4  # room for the squares between circles
WEEKLY_CIRCLE_SQUARE_MARGIN_PIXELS = 1.5  # how far the "should be left" square reaches past its circle
WEEKLY_CIRCLE_SQUARE_COLOR = "#6a6a6a"  # light gray
WEEKLY_CIRCLE_LEFT_COLOR = "#3dd6c6"  # teal
WEEKLY_CIRCLE_IN_USE_COLOR = "#e8e8e8"  # white: the circle being used now, and its bar
WEEKLY_IN_USE_BAR_OUTLINE_PIXELS = 2
WEEKLY_IN_USE_BAR_TRACK_COLOR = "#4a4a4a"  # the empty part of the bar, visible on the dark background
WEEKLY_CIRCLE_USED_UP_COLOR = "#333333"  # dark gray, so it stands out on the light gray "should be left" square
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


class ThinBar:
    """A thin progress bar the width of the floating label: a filled part over a dark track."""

    def __init__(self, tk_root, fill_color):
        self.fill_color = fill_color
        self.filled_fraction = None
        self.canvas = tkinter.Canvas(
            tk_root,
            width=1,  # let the text set the label's width; the bar stretches to match
            height=BAR_HEIGHT_PIXELS,
            background="#3a3a3a",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="x")
        self.filled_part = self.canvas.create_rectangle(0, 0, 0, BAR_HEIGHT_PIXELS, fill=fill_color, width=0)
        self.canvas.bind("<Configure>", lambda event: self.redraw())

    def show_fraction(self, filled_fraction, fill_color=None):
        self.filled_fraction = filled_fraction
        if fill_color is not None:
            self.fill_color = fill_color
        self.redraw()

    def redraw(self):
        fraction = 0 if self.filled_fraction is None else max(0.0, min(1.0, self.filled_fraction))
        self.canvas.coords(self.filled_part, 0, 0, round(self.canvas.winfo_width() * fraction), BAR_HEIGHT_PIXELS)
        self.canvas.itemconfigure(self.filled_part, fill=self.fill_color)


class WeeklyDayCircles:
    """7 small circles left of "⚡", in a 3 x 3 grid filled column by column (3 + 3 + 1): the weekly limit LEFT, split
    into 7 equal parts (one per day, about 14.3% each). All teal at the start of the week; they empty from the last
    one back as the week is used. The one in use now is white, and the grid's empty corner (bottom right, 2 slots tall)
    shows how much of it is left as a white bar. Used-up parts are dark gray.

    A light gray square behind the circles marks those you SHOULD still have to last the week: one per day left until
    the weekly reset (7 on the first day, 6 on the second, ...). Filled circles without a square are spare; squares
    behind gray circles mean you are using it faster than one part a day."""

    def __init__(self, parent_widget):
        # The squares reach past the circles, so the grid keeps a margin for them.
        self.grid_size_pixels = (
            WEEKLY_CIRCLE_GRID_SIZE * WEEKLY_CIRCLE_DIAMETER_PIXELS
            + (WEEKLY_CIRCLE_GRID_SIZE - 1) * WEEKLY_CIRCLE_GAP_PIXELS
            + round(2 * WEEKLY_CIRCLE_SQUARE_MARGIN_PIXELS)
        )
        # A picture drawn with Pillow rather than tkinter canvas shapes: tkinter draws without smoothing, which made
        # circles this small look jagged and fuzzy.
        self.circles_photo = None  # tkinter shows the picture only while Python keeps a reference to it
        self.widget = tkinter.Label(parent_widget, background="#222222", borderwidth=0)
        self.widget.pack(side="right", padx=(5, 0))
        self.show_weekly_percent_used(None, None)

    @staticmethod
    def grid_slot_top_left(column_number, row_number):
        """Top-left corner, in real pixels, of a slot in the 3 x 3 grid."""
        return (
            WEEKLY_CIRCLE_SQUARE_MARGIN_PIXELS + column_number * (WEEKLY_CIRCLE_DIAMETER_PIXELS + WEEKLY_CIRCLE_GAP_PIXELS),
            WEEKLY_CIRCLE_SQUARE_MARGIN_PIXELS + row_number * (WEEKLY_CIRCLE_DIAMETER_PIXELS + WEEKLY_CIRCLE_GAP_PIXELS),
        )

    def draw_circles_image(self, parts_left, parts_that_should_be_left):
        """Draw at WEEKLY_CIRCLE_DRAWING_SCALE times the size, then shrink: the shrinking smooths the edges."""
        scale = WEEKLY_CIRCLE_DRAWING_SCALE
        big_image = Image.new("RGB", (self.grid_size_pixels * scale,) * 2, "#222222")
        draw = ImageDraw.Draw(big_image)
        fraction_of_part_in_use_left = None
        for part_number in range(WEEKLY_PART_COUNT):
            circle_left, circle_top = self.grid_slot_top_left(*divmod(part_number, WEEKLY_CIRCLE_GRID_SIZE))
            circle_box = [
                circle_left * scale,
                circle_top * scale,
                (circle_left + WEEKLY_CIRCLE_DIAMETER_PIXELS) * scale - 1,
                (circle_top + WEEKLY_CIRCLE_DIAMETER_PIXELS) * scale - 1,
            ]
            # Behind the circles that should still be left: a light gray square.
            if part_number < parts_that_should_be_left:
                square_margin = WEEKLY_CIRCLE_SQUARE_MARGIN_PIXELS * scale
                draw.rounded_rectangle(
                    [
                        circle_box[0] - square_margin,
                        circle_box[1] - square_margin,
                        circle_box[2] + square_margin,
                        circle_box[3] + square_margin,
                    ],
                    radius=scale,
                    fill=WEEKLY_CIRCLE_SQUARE_COLOR,
                )
            fraction_of_this_part_left = max(0.0, min(1.0, parts_left - part_number))
            if fraction_of_this_part_left >= 1:
                circle_color = WEEKLY_CIRCLE_LEFT_COLOR
            elif fraction_of_this_part_left > 0:
                circle_color = WEEKLY_CIRCLE_IN_USE_COLOR
                fraction_of_part_in_use_left = fraction_of_this_part_left
            else:
                circle_color = WEEKLY_CIRCLE_USED_UP_COLOR
            draw.ellipse(circle_box, fill=circle_color)

        # The empty corner (column 3, rows 2-3): a white bar, full from the bottom, for how much of the white circle
        # is left.
        if fraction_of_part_in_use_left is not None:
            # As wide as a gray square and as tall as two, so it fills the corner.
            square_margin = WEEKLY_CIRCLE_SQUARE_MARGIN_PIXELS
            bar_left, bar_top = self.grid_slot_top_left(2, 1)
            bar_bottom = self.grid_slot_top_left(2, 2)[1] + WEEKLY_CIRCLE_DIAMETER_PIXELS
            bar_box = [
                (bar_left - square_margin) * scale,
                (bar_top - square_margin) * scale,
                (bar_left + WEEKLY_CIRCLE_DIAMETER_PIXELS + square_margin) * scale - 1,
                (bar_bottom + square_margin) * scale - 1,
            ]
            # A white outline shows the bar's full height, like a battery.
            draw.rounded_rectangle(
                bar_box,
                radius=scale,
                fill=WEEKLY_IN_USE_BAR_TRACK_COLOR,
                outline=WEEKLY_CIRCLE_IN_USE_COLOR,
                width=WEEKLY_IN_USE_BAR_OUTLINE_PIXELS * scale,
            )
            filled_top = bar_box[3] - (bar_box[3] - bar_box[1]) * fraction_of_part_in_use_left
            draw.rounded_rectangle(
                [bar_box[0], filled_top, bar_box[2], bar_box[3]], radius=scale, fill=WEEKLY_CIRCLE_IN_USE_COLOR
            )
        return big_image.resize((self.grid_size_pixels,) * 2, Image.LANCZOS)

    def show_weekly_percent_used(self, weekly_percent_used, minutes_left_in_week):
        """None (unknown) shows all gray, or no squares."""
        if minutes_left_in_week is None:
            parts_that_should_be_left = 0
        else:
            # Days left, rounded up: on the first day (6 days and some hours to go) that is 7.
            parts_that_should_be_left = min(WEEKLY_PART_COUNT, math.ceil(minutes_left_in_week / (24 * 60)))
        if weekly_percent_used is None:
            parts_left = 0
        else:
            # Counted in parts (e.g. 37% used = 4.41 parts left) rather than in % per part, which is 14.2857… and
            # leaves rounding errors at 0% and 100%.
            parts_left = (100 - weekly_percent_used) * WEEKLY_PART_COUNT / 100
        self.circles_photo = ImageTk.PhotoImage(self.draw_circles_image(parts_left, parts_that_should_be_left))
        self.widget.configure(image=self.circles_photo)


class FloatingUsageLabel:
    """A small borderless always-on-top text window. Drag it with the left mouse button; right-click for a menu."""

    def __init__(self, tk_root, on_quit_requested, on_login_requested, on_switch_account_requested):
        self.tk_root = tk_root
        tk_root.overrideredirect(True)  # no title bar or border
        tk_root.attributes("-topmost", True)
        tk_root.attributes("-alpha", 0.9)
        tk_root.configure(background="#222222")
        # Top bar (blue): time left in the 5-hour window, full at 5h left and empty at 0.
        self.time_left_bar = ThinBar(tk_root, TIME_LEFT_COLOR)

        # The text row: "⚡ 47%" in the usage bar's color first, then "⏳ 2h41m" in blue (matching the time bar).
        text_row = tkinter.Frame(tk_root, background="#222222", padx=6, pady=4)
        text_row.pack()
        # At the right end, packed from the right before anything else: the account name at the very edge, then the
        # weekly limit as 7 circles in a 3 x 3 grid just left of it.
        # Which account this is, e.g. "dev"; shown only when there is more than one account. Click it to
        # switch to the next account (so it is not a drag handle).
        self.account_name_text = tkinter.Label(
            text_row,
            text="",
            font=("Segoe UI", 8, "bold"),
            foreground="#999999",
            background="#222222",
            cursor="hand2",
        )
        self.account_name_text.pack(side="right", padx=(4, 0))
        self.account_name_text.bind("<Button-1>", lambda event: on_switch_account_requested())
        self.weekly_day_circles = WeeklyDayCircles(text_row)
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

        # Bottom bar: usage left, full when nothing is used and empty at 100%; green / orange / red by usage.
        self.usage_left_bar = ThinBar(tk_root, "#bbbbbb")

        self.drag_start_offset = (0, 0)
        self.user_has_moved_label = False
        right_click_menu = tkinter.Menu(tk_root, tearoff=False)
        right_click_menu.add_command(label="Hide label (click the tray icon to bring it back)", command=self.hide)
        right_click_menu.add_command(label="Quit", command=on_quit_requested)
        self.account_name_text.bind(
            "<Button-3>", lambda event: right_click_menu.tk_popup(event.x_root, event.y_root)
        )
        for draggable_widget in (
            self.weekly_day_circles.widget,
            text_row,
            self.time_left_text,
            self.time_of_last_drop_text,
            self.usage_percent_text,
            self.usage_last_drop_text,
            *self.text_slot_frames.values(),  # the empty space inside a slot drags too
        ):
            draggable_widget.bind("<ButtonPress-1>", self.start_drag)
            draggable_widget.bind("<B1-Motion>", self.continue_drag)
            draggable_widget.bind(
                "<Button-3>", lambda event: right_click_menu.tk_popup(event.x_root, event.y_root)
            )

        self.move_to_bottom_right_above_taskbar()
        self.keep_on_top_forever()

    def move_to_bottom_right_above_taskbar(self):
        """Place it at the bottom-right of the main monitor's work area (the screen minus the taskbar).

        Uses the real window and real screen positions from Windows rather than tkinter's geometry, because with two
        monitors at different scaling tkinter's placement sometimes landed on the second monitor."""
        self.tk_root.update_idletasks()
        window_handle = ctypes.windll.user32.GetAncestor(self.tk_root.winfo_id(), GA_ROOT)
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
        self.weekly_day_circles.show_weekly_percent_used(weekly_percent_used, minutes_left_in_week)
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

    def show_account_name(self, account_name, all_account_names):
        """An empty name hides it (and its gap). Its slot fits the longest account name, so switching accounts doesn't
        change the label's width ("fin" and "dev" are not equally wide)."""
        name_font_measurer = tkinter.font.Font(font=self.account_name_text.cget("font"))
        account_name = account_name.upper()  # "FIN" / "DEV"
        longest_name = max((name.upper() for name in all_account_names), key=name_font_measurer.measure)
        self.account_name_text.configure(text=longest_name)
        self.account_name_text.update_idletasks()
        # The label's own width counts characters, so size its text in pixels with padding instead.
        extra_pixels = self.account_name_text.winfo_reqwidth() - name_font_measurer.measure(account_name or "")
        self.account_name_text.configure(text=account_name, width=0)
        self.account_name_text.pack_configure(padx=(4 + extra_pixels, 0) if account_name else 0)

    def hide(self):
        self.tk_root.withdraw()

    def show_at_default_position(self):
        self.tk_root.deiconify()
        self.user_has_moved_label = False
        self.move_to_bottom_right_above_taskbar()

    def keep_on_top_forever(self):
        """Re-applied 4 times a second: Windows can push it down (a taskbar click puts the taskbar above it), and can
        move it (to the second monitor), so it is raised and, unless dragged by hand, put back in its corner."""
        if self.tk_root.state() != "withdrawn":
            self.tk_root.attributes("-topmost", False)
            self.tk_root.attributes("-topmost", True)
            self.tk_root.lift()
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
            on_switch_account_requested=self.switch_to_next_account,
        )
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

    def switch_to_next_account(self):
        """Clicking the account name on the label: go to the next account (with two, that toggles between them)."""
        all_accounts = list_claude_accounts()
        account_names = [account["name"] for account in all_accounts]
        current_position = account_names.index(self.selected_account["name"]) if (
            self.selected_account["name"] in account_names
        ) else -1
        self.select_account(all_accounts[(current_position + 1) % len(all_accounts)])

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
            # After the weekly reset nothing of the new week is used yet: all circles full.
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

    def open_claude_login_window(self):
        """Open a console window running "claude auth login" (it opens the browser to sign in), then fetch again
        once that window closes. You sign in yourself; the app never sees your password."""
        if self.claude_login_window_process and self.claude_login_window_process.poll() is None:
            return  # already open
        # The selected account's folder: CLAUDE_CONFIG_DIR makes Claude Code keep that login there.
        login_environment = dict(os.environ)
        login_environment.pop("CLAUDE_CONFIG_DIR", None)
        if Path(self.selected_account["folder"]) != DEFAULT_CLAUDE_CONFIG_FOLDER:
            login_environment["CLAUDE_CONFIG_DIR"] = self.selected_account["folder"]
        self.claude_login_window_process = subprocess.Popen(
            [
                "cmd.exe",
                "/c",
                f"title Claude login: {self.selected_account['name']} & claude auth login & timeout /t 10",
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
        self.tk_root.after(1000, self.check_requests_every_second, seconds_since_last_refresh + 1)

    def watch_for_tray_icon_clicks(self):
        """Runs on the tkinter loop 5 times a second, so clicking the tray icon moves the label back right away."""
        if self.show_label_requested_event.is_set():
            self.show_label_requested_event.clear()
            self.floating_label.show_at_default_position()
        if self.account_changed_event.is_set():
            self.account_changed_event.clear()
            show_name = len(list_claude_accounts()) > 1
            self.floating_label.show_account_name(
                self.selected_account["name"] if show_name else "",
                [account["name"] for account in list_claude_accounts()],
            )
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
                save_usage_snapshot(fetch_live_usage_snapshot(self.selected_account["folder"]))
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
