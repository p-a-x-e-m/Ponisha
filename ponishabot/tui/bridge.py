"""Pure logic behind the TUI — no terminal, no widgets, no I/O.

Everything here takes plain values and returns plain values, so it can be tested
without a running app. The TUI screens import from this module rather than
carrying their own copies, and the tests exercise this module rather than the
widgets.

The log helpers came from `ponishabot/panel.py`, which the TUI replaced. They
were already covered by tests and their behaviour was right, so they moved rather
than being rewritten: the project's log repeats `Forex fetch failed` indefinitely,
and filtering that out is what makes the log pane readable at all.
"""

import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

# ── Palette, taken from dashboard/static/dashboard/css/app.css so the terminal
# and the web dashboard read as one product rather than two.
ACCENT = "#f5a524"   # --accent
OK = "#3ddc84"       # --ok
DANGER = "#ff5c5c"   # --danger
INFO = "#4cc2ff"     # --info
MUTED = "#9aa3ad"    # --muted
FG = "#e8eaed"       # --fg
BORDER = "#2a3138"   # --border-strong

# Log lines that are expected during normal operation. Hidden by default.
NOISE_PATTERNS = (
    "Forex fetch failed",
    "WebDriver manager",
    "Get LATEST chromedriver",
    "ChromeDriver from cache",
    "WebDriver initialized",
    "usd refresh failed",
    "About to download new driver",
    "Driver downloading response",
    "Modern chrome version",
    "WebDriver version",
    "no [win64] chromedriver",
    "Settings saved to",
)

# Lines that say what the bot is actually DOING. Severity alone is a poor filter
# here: the interesting lines are logged at INFO while the chattiest noise is at
# WARNING, so the default view is "WARNING and above, plus anything that says
# what the bot is doing".
EVENT_PATTERNS = (
    "project(s) posted in the last",
    "match notify sent",
    "Bid submitted successfully",
    "Bid confirmed",
    "Bid unconfirmed",
    "Bid failed",
    "Bid aborted",
    "Bidder not attached",
    "Rejected:",
    "AI: bid",
    "AI: no bid",
    "— heuristic fallback",
    "Karlancer market:",
    "Proposal quota:",
    "Monitor started",
    "Monitor stopped",
    "Stop requested",
    "No new matching project",
    "aborting remaining projects",
    "Loaded",
    "Session rebuilt from stored OTP",
    "Cookies loaded from database",
)

# Severity ranking for the log filter, lowest first.
LEVELS = ("debug", "info", "important")

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - (\w+) - (.*)$")


# ──────────────────────────────────────────────────────────────────────────
# Log
# ──────────────────────────────────────────────────────────────────────────

def strip_ansi(text: str) -> str:
    """Drop ANSI escapes so a coloured line can be matched and measured."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def classify_line(line: str) -> Tuple[str, str, str]:
    """Split a log line into (level, timestamp, message).

    Falls back to ("info", "", line) for anything that does not match the logging
    format — a traceback continuation, a banner, or another writer's line.
    """
    clean = strip_ansi(line).rstrip()
    m = _TS_RE.match(clean)
    if not m:
        return ("info", "", clean)
    ts, level, msg = m.groups()
    return (level.upper(), ts[11:], msg)   # ts[11:] is HH:MM:SS


def is_noise(message: str) -> bool:
    """True for a known-benign line that would otherwise repeat forever."""
    return any(p in message for p in NOISE_PATTERNS)


def is_event(message: str) -> bool:
    """True for a line that describes bot activity worth showing by default."""
    return any(p in message for p in EVENT_PATTERNS)


def collapse_repeats(lines: Sequence[str]) -> List[Tuple[str, int]]:
    """Group consecutive identical messages into (text, count) pairs.

    Identity is the *message* alone. Comparing whole lines would never match
    anything, because the timestamp differs on every repetition — which is exactly
    the shape of the run this exists to collapse (the project's log had
    `Forex fetch failed` nine times, at nine different seconds).

    The first occurrence's timestamp is shown: it marks when the run started, and
    the count says how long it lasted.
    """
    out: List[Tuple[str, int]] = []
    keys: List[str] = []
    for line in lines:
        _level, ts, msg = classify_line(line)
        if out and keys[-1] == msg:
            text, n = out[-1]
            out[-1] = (text, n + 1)
        else:
            keys.append(msg)
            out.append((f"{ts} {msg}".strip() if ts else msg, 1))
    return out


def filter_lines(lines: Sequence[str], level: str = "important",
                 show_noise: bool = False) -> List[str]:
    """Apply the display policy to raw log lines.

    `level` is one of LEVELS. "important" keeps WARNING and above *plus* the
    lines matching EVENT_PATTERNS; "info" adds the rest of INFO; "debug" shows
    everything. Noise is removed unless `show_noise`.
    """
    if level not in LEVELS:
        level = "important"
    min_rank = LEVELS.index(level)

    kept: List[str] = []
    for line in lines:
        lvl, _ts, msg = classify_line(line)
        if not show_noise and is_noise(msg):
            continue
        if lvl in ("WARNING", "ERROR", "CRITICAL"):
            rank = 2
        elif lvl == "DEBUG":
            rank = 0
        else:
            rank = 1          # INFO, and anything the format did not match

        # An INFO line describing bot activity outranks a WARNING about the
        # network. Without this the default view is nearly empty during a healthy
        # run — plenty of warnings, almost no INFO — so the panel would show
        # nothing happening while the bot was working fine.
        if rank < min_rank and is_event(msg):
            rank = min_rank

        if rank >= min_rank:
            kept.append(line)
    return kept


# ──────────────────────────────────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────────────────────────────────

def fmt_money(value) -> str:
    """Group digits with a thin separator, or an em dash when unknown."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}".replace(",", "٬")
    except (TypeError, ValueError):
        return "—"


def fmt_duration(seconds) -> str:
    """Compact uptime: 3h 07m, 12m 40s, 8s."""
    if not seconds or seconds < 0:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def fmt_quota(remaining) -> str:
    return "—" if remaining is None else str(remaining)


def layout_for_width(width: int) -> str:
    """Which arrangement fits: "wide" (side by side) or "narrow" (stacked).

    Textual reflows on its own, but the dashboard's two status cards read better
    stacked in a narrow window than squeezed side by side.
    """
    return "wide" if width >= 86 else "narrow"


def truncate_width(text: str, room: int) -> str:
    """Clip `text` to `room` display columns, marking the cut.

    Measured in cells rather than characters: a Persian or CJK line is not as wide
    as len() suggests, so a character count would clip the wrong amount.
    """
    from rich.cells import cell_len
    if cell_len(text) <= room:
        return text
    cut = text
    while cut and cell_len(cut) > max(1, room - 1):
        cut = cut[:-1]
    return cut + "…"


def status_rows(snap: Dict) -> List[Tuple[str, str]]:
    """Turn a status snapshot into (label, value) rows for display."""
    running = bool(snap.get("running"))
    if snap.get("bidding"):
        state = "در حال ارسال پیشنهاد"
    elif running:
        state = "در حال اجرا"
    else:
        state = "متوقف"
    return [
        ("وضعیت", state),
        ("سهمیه", fmt_quota(snap.get("quota"))),
        ("متچ", str(snap.get("matches", 0))),
        ("بید", str(snap.get("bids", 0))),
        ("Uptime", fmt_duration(snap.get("uptime"))),
        ("AI", "فعال" if snap.get("ai_enabled") else "خاموش"),
        ("فاصله", f"{snap.get('refresh_interval', '?')}s"),
        ("جلسه", "معتبر" if snap.get("has_cookies") else "نامعتبر"),
    ]


# ──────────────────────────────────────────────────────────────────────────
# Settings validation
# ──────────────────────────────────────────────────────────────────────────

# Bounds the dashboard's api_settings enforces, so a value typed into the TUI
# cannot land in config.yaml with something the dashboard would have refused —
# an interval of 0, or a NaN, poisons every later comparison.
_NUMERIC_RANGES = {
    "refresh_interval": (10, 3600),
    "budget_min": (0, None),
    "budget_max": (0, None),
    "hourly_min": (0, None),
    "hourly_max": (0, None),
    "min_remaining_proposals": (0, None),
    "max_pages": (1, 200),
    "priority_window_minutes": (0, 1440),
    "high_competition_threshold": (0, None),
    "urgency_premium_percent": (0, 100),
    "high_competition_discount_percent": (0, 100),
    "low_competition_discount_percent": (0, 100),
    "ai_max_description_chars": (100, 100000),
    "karlancer_max_pages": (1, 50),
}

# Values that must stay inside 0–1.
_UNIT_RANGES = (
    "min_skill_match",
    "ai_min_skill_gate",
    "ai_temperature",
    "forex_sensitivity",
)

_BOOL_KEYS = (
    "dry_run", "headless", "show_browser", "ai_enabled", "karlancer_enabled",
    "forex_enabled", "kl_clamp_enabled",
)


class InvalidSetting(ValueError):
    """A value the dashboard would have rejected."""


def validate_setting(key: str, raw):
    """Coerce and bound one setting, or raise InvalidSetting.

    Mirrors the checks in `dashboard/views.py:api_settings` — the dashboard
    already refuses NaN, infinities and out-of-range numbers. The TUI writes to
    the same config.yaml, so it has to refuse them too rather than let a typed
    value poison the file for every later run.
    """
    if key in _BOOL_KEYS:
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "on", "yes")
        return bool(raw)

    if key in _NUMERIC_RANGES or key in _UNIT_RANGES:
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise InvalidSetting(f"{key}: عدد لازم است")
        if num != num or num in (float("inf"), float("-inf")):
            raise InvalidSetting(f"{key}: عدد نامعتبر")
        lo, hi = _NUMERIC_RANGES.get(key, (0.0, 1.0))
        if lo is not None and num < lo:
            num = lo
        if hi is not None and num > hi:
            num = hi
        if key in _UNIT_RANGES:
            return num
        return int(num)

    if key == "skills":
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return list(raw or [])

    return str(raw)


def settings_from_form(form: Dict, cfg) -> Dict:
    """Build the kwargs for `BotApp.set_*` from a settings form.

    Kept separate from the widgets so the mapping is testable: an unknown or
    malformed entry must raise rather than reach config.yaml.
    """
    out: Dict = {}
    for key, raw in form.items():
        out[key] = validate_setting(key, raw)
    return out
