"""Interactive terminal panel for `python main.py`.

The web dashboard has always been the richer way to drive the bot; the terminal
path only ever dumped the log stream to the console, so a headless run gave no
live status and no way to stop, start or flip dry_run without killing the
process. This module is the terminal equivalent: a live panel that shows the
same state the dashboard does and takes the same controls.

Design constraints that shaped it:

- **Runs on any terminal.** The user works across Windows Terminal, cmd,
  PowerShell, VS Code and Git Bash, so the panel degrades instead of breaking:
  no colour, a narrow window, or a `rich` that is not installed each fall
  back to something usable rather than an exception.
- **Never blocks the monitor.** The panel owns the main thread (it reads
  keystrokes) and the monitor runs in a daemon thread, exactly as it does for
  the web dashboard in `BotApp.start`.
- **Pure logic stays out of the terminal.** `collapse_repeats`, `classify_line`
  and the formatters below take strings and numbers and return strings, so
  they are testable without a TTY. See `tests/test_ponishabot.py`.
"""

import logging
import os
import queue
import re
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Palette, taken from dashboard/static/dashboard/css/app.css so the two
# interfaces read as one product rather than two.
ACCENT = "#f5a524"   # --accent
OK = "#3ddc84"       # --ok
DANGER = "#ff5c5c"   # --danger
INFO = "#4cc2ff"     # --info
MUTED = "#9aa3ad"    # --muted
FG = "#e8eaed"       # --fg

# Log lines that are expected during normal operation. The project's own log
# showed `Forex fetch failed` nine times in a row while cycling, which buries
# everything worth reading. Hidden by default; `v` shows them.
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

# Events that say what the bot is actually DOING. Severity alone is a poor
# filter here: the interesting lines are logged at INFO, while the chattiest
# noise is logged at WARNING. So the default view is "WARNING and above, plus
# anything matching this list", which is what a person watching a run wants.
EVENT_PATTERNS = (
    "project(s) posted in the last",   # priority batch about to be bid
    "match notify sent",               # a real match
    "Bid submitted successfully",
    "Bid confirmed",
    "Bid unconfirmed",
    "Bid failed",
    "Bid aborted",
    "Bidder not attached",
    "Rejected:",                       # why a project was skipped
    "AI: bid",                         # the model's decision, with price
    "AI: no bid",
    "— heuristic fallback",            # mid-run fallback; the startup line
                                       # "Decision engine: AI (respone) with
                                       # heuristic fallback" is configuration,
                                       # not an event, and must not match
    "Karlancer market:",
    "Proposal quota:",
    "Monitor started",
    "Monitor stopped",
    "Stop requested",
    "No new matching project",
    "aborting remaining projects",
    "Loaded",                          # persisted bid count at startup
    "Session rebuilt from stored OTP",
    "Cookies loaded from database",
)

# Severity ranking for the log filter, lowest first.
LEVELS = ("debug", "info", "important")

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - (\w+) - (.*)$")


def is_event(message: str) -> bool:
    """True for a line that describes bot activity worth showing by default."""
    return any(p in message for p in EVENT_PATTERNS)


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers — no terminal state, no I/O. These are what the tests exercise.
# ──────────────────────────────────────────────────────────────────────────

def strip_ansi(text: str) -> str:
    """Drop ANSI escapes so a coloured line can be matched and measured."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def classify_line(line: str) -> Tuple[str, str, str]:
    """Split a log line into (level, timestamp, message).

    Falls back to ("info", "", line) for anything that does not match the
    logging format — a traceback continuation, a banner, or a line from
    another writer.
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


def collapse_repeats(lines: List[str]) -> List[Tuple[str, int]]:
    """Group consecutive identical messages into (text, count) pairs.

    Identity is the *message* alone. Comparing the whole line would never match
    anything, because the timestamp differs on every repetition — which is
    exactly the shape of the run this exists to collapse (the project's log had
    `Forex fetch failed` nine times, at nine different seconds).

    The first occurrence's timestamp is what gets shown: it marks when the run
    started, and the count says how long it lasted.
    """
    out: List[Tuple[str, int]] = []
    keys: List[str] = []          # the message each output row was built from
    for line in lines:
        _level, ts, msg = classify_line(line)
        if out and keys[-1] == msg:
            text, n = out[-1]
            out[-1] = (text, n + 1)
        else:
            keys.append(msg)
            out.append((f"{ts} {msg}".strip() if ts else msg, 1))
    return out


def filter_lines(lines: List[str], level: str = "important",
                 show_noise: bool = False) -> List[str]:
    """Apply the panel's display policy to raw log lines.

    `level` is one of LEVELS. "important" keeps WARNING and above; "info" adds
    INFO; "debug" shows everything. Noise is removed unless `show_noise`.
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
        # network. Without this the default view is nearly empty during a
        # healthy run — plenty of warnings, almost no INFO — so the panel would
        # show nothing happening while the bot was working fine.
        if rank < min_rank and is_event(msg):
            rank = min_rank

        if rank >= min_rank:
            kept.append(line)
    return kept


def fmt_money(value: Optional[int]) -> str:
    """Group digits with a thin separator, or an em dash when unknown."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}".replace(",", "٬")
    except (TypeError, ValueError):
        return "—"


def fmt_duration(seconds: Optional[float]) -> str:
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


def fmt_quota(remaining: Optional[int], interval: int = 0) -> str:
    """Quota with a rough runway estimate when a poll interval is known."""
    if remaining is None:
        return "—"
    return str(remaining)


def layout_for_width(width: int) -> str:
    """Which arrangement fits: "wide" (side by side) or "narrow" (stacked)."""
    return "wide" if width >= 86 else "narrow"


# ──────────────────────────────────────────────────────────────────────────
# Keystroke reading — non-blocking, per platform
# ──────────────────────────────────────────────────────────────────────────

def read_key(timeout: float = 0.0) -> Optional[str]:
    """Return one lowercased keypress, or None if nothing is waiting.

    Windows uses msvcrt; everywhere else uses termios + select. Both return
    immediately, so the panel's render loop is never blocked by input.
    """
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return None
        if msvcrt.kbhit():
            try:
                ch = msvcrt.getch()
            except OSError:
                return None
            try:
                return ch.decode("utf-8", "ignore").lower()
            except Exception:
                return None
        return None

    try:
        import select
        import termios
        import tty
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        return None
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        return sys.stdin.read(1).lower()
    except Exception:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# The panel
# ──────────────────────────────────────────────────────────────────────────

class TerminalPanel:
    """Live status + controls in the terminal, driving the shared BotApp.

    The app is the process-wide singleton from `botstate.get_app()`, never a
    fresh `BotApp`: a second instance would carry its own Selenium driver and
    its own monitor, and `status_snapshot()` would report on the wrong one.
    """

    RENDER_INTERVAL = 0.5      # seconds between redraws
    KEY_TIMEOUT = 0.05         # how long to wait for a key each pass

    def __init__(self, app, level: str = "important"):
        self.app = app
        self.level = level
        self.show_noise = False
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue(maxsize=500)
        self._recent: List[Tuple[str, str, Optional[int]]] = []   # (title, price, days)
        self._log_offset = 0
        self._log_buf: List[str] = []
        self._notice = ""
        self._notice_until = 0.0
        self._monitor_thread: Optional[threading.Thread] = None

    # ── events ──
    def observe(self, event: dict) -> None:
        """Monitor observer. Must never raise: a broken observer would stop the
        monitor loop (see ProjectMonitor.emit, which swallows around callbacks,
        but we stay defensive anyway)."""
        try:
            self._events.put_nowait(event)
        except Exception:
            pass

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            kind = ev.get("type")
            if kind == "new_project":
                project = ev.get("project")
                title = getattr(project, "title", "") or ""
                self._recent.insert(0, (title, ev.get("price"), ev.get("duration_days")))
                del self._recent[6:]
                self._notice = f"پیشنهاد جدید: {title[:40]}"
                self._notice_until = time.time() + 6
            elif kind == "quota_low":
                self._notice = f"سهمیه کم شد: {ev.get('remaining')}"
                self._notice_until = time.time() + 10

    # ── log ──
    def _read_new_log(self) -> None:
        from ponishabot import botstate
        try:
            chunk = botstate.read_log_tail(since_byte=self._log_offset)
        except Exception:
            return
        self._log_offset = chunk.get("since", self._log_offset)
        text = chunk.get("text") or ""
        if not text:
            return
        for line in text.splitlines():
            if line.strip():
                self._log_buf.append(line)
        # Keep the buffer bounded; the panel only ever shows the tail.
        if len(self._log_buf) > 2000:
            del self._log_buf[:-1000]

    # ── render ──
    def _status_line(self, snap: Dict) -> str:
        if snap.get("bidding"):
            return "[#4cc2ff]● در حال ارسال پیشنهاد[/]"
        if snap.get("running"):
            return f"[{OK}]● در حال اجرا[/]"
        return f"[{MUTED}]○ متوقف[/]"

    def _build(self, snap: Dict, width: int):
        """Assemble the renderable. Imported lazily so this module imports
        cleanly even when rich is absent (the caller then falls back)."""
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        started = snap.get("started_at")
        uptime = fmt_duration(time.time() - started) if started else "—"
        quota = fmt_quota(snap.get("quota"))

        head = Table.grid(padding=(0, 2))
        head.add_column(justify="left")
        head.add_column(justify="left")
        head.add_row(self._status_line(snap),
                     f"[{FG}]سهمیه[/] [{ACCENT}]{quota}[/]"
                     f"  [{MUTED}]·[/]  [{FG}]متچ[/] [{FG}]{snap.get('matches', 0)}[/]"
                     f"  [{MUTED}]·[/]  [{FG}]بید[/] [{FG}]{snap.get('bids', 0)}[/]")
        head.add_row(
            f"[{MUTED}]AI {'فعال' if snap.get('ai_enabled') else 'خاموش'}"
            f" · فاصله {snap.get('refresh_interval', '?')}s"
            f" · جلسه {'معتبر' if snap.get('has_cookies') else 'نامعتبر'}[/]",
            f"[{MUTED}]Uptime {uptime}[/]")

        body = [head]

        if self._recent:
            t = Table.grid(padding=(0, 2))
            t.add_column(justify="right", style=ACCENT)
            t.add_column(style=FG, overflow="ellipsis", max_width=max(20, width - 30))
            for title, price, days in self._recent[:5]:
                suffix = f" [dim]· {days} روز[/]" if days else ""
                t.add_row(fmt_money(price), f"{title}{suffix}")
            body.append(Text(""))
            body.append(Text.from_markup(f"[{MUTED}]آخرین پیشنهادها[/]"))
            body.append(t)

        shown = filter_lines(self._log_buf[-400:], level=self.level,
                            show_noise=self.show_noise)
        collapsed = collapse_repeats(shown)[-12:]
        if collapsed:
            body.append(Text(""))
            body.append(Text.from_markup(
                f"[{MUTED}]لاگ[/] [dim]({self.level}{'، +نویز' if self.show_noise else ''})[/]"))
            for text, count in collapsed:
                # Truncate by display width, not by character count: a Persian
                # or CJK line has a different visual length than len() suggests.
                from rich.cells import cell_len
                room = max(20, width - 8)
                if cell_len(text) > room:
                    # Cut on the width budget, then mark it so a clipped line
                    # cannot be mistaken for the whole message.
                    cut = text
                    while cut and cell_len(cut) > room - 1:
                        cut = cut[:-1]
                    text = cut + "…"
                # Built with Text rather than from_markup: log messages contain
                # brackets (URLs, [win64]), and rich would try to parse those as
                # markup and raise.
                row = Text(text, style=MUTED)
                if count > 1:
                    row.append(f"  ×{count}", style="dim")
                body.append(row)

        if self._notice and time.time() < self._notice_until:
            body.append(Text(""))
            body.append(Text.from_markup(f"[{ACCENT}]{self._notice}[/]"))

        keys = (f"[{ACCENT}]s[/] {'توقف' if snap.get('running') else 'شروع'}"
                f"  [{ACCENT}]d[/] dry-run={'روشن' if snap.get('dry_run') else 'خاموش'}"
                f"  [{ACCENT}]l[/] ورود"
                f"  [{ACCENT}]v[/] لاگ"
                f"  [{ACCENT}]c[/] پاک‌کردن"
                f"  [{ACCENT}]q[/] خروج")
        return Panel(Group(*body),
                     title=f"[{ACCENT}]ponishabot[/]",
                     subtitle=f"[{DANGER}]DRY_RUN[/]" if snap.get("dry_run") else None,
                     border_style=ACCENT,
                     padding=(0, 1)), keys

    # ── controls ──
    def _handle_key(self, key: str, console) -> bool:
        """Act on a keypress. Returns False when the panel should exit."""
        app = self.app
        if key in ("q", "\x03"):        # q or Ctrl+C
            return False
        if key == "s":
            if app.is_running():
                self._notice, self._notice_until = "در حال توقف…", time.time() + 5
                threading.Thread(target=app.stop, daemon=True,
                                 name="panel-stop").start()
            else:
                self._notice, self._notice_until = "در حال شروع…", time.time() + 5
                threading.Thread(target=lambda: app.start(interactive=False),
                                 daemon=True, name="panel-start").start()
        elif key == "d":
            app.set_dry_run(not app.cfg.dry_run)
            self._notice = f"dry-run {'روشن' if app.cfg.dry_run else 'خاموش'} شد"
            self._notice_until = time.time() + 4
        elif key == "l":
            self._notice, self._notice_until = "پنجرهٔ ورود باز می‌شود…", time.time() + 6
            threading.Thread(target=app.open_login_browser, daemon=True,
                             name="panel-login").start()
        elif key == "v":
            i = LEVELS.index(self.level)
            self.level = LEVELS[(i + 1) % len(LEVELS)]
        elif key == "c":
            self._log_buf.clear()
            self._notice, self._notice_until = "لاگ پاک شد", time.time() + 3
        return True

    # ── loop ──
    def run(self) -> int:
        """Own the main thread until the user quits. Returns an exit code."""
        from ponishabot import botstate

        app = self.app
        if app.monitor is not None:
            app.monitor.add_observer(self.observe)

        # Start the monitor in the background unless the caller already did.
        if not app.is_running():
            self._notice = "در حال شروع مانیتور…"
            self._notice_until = time.time() + 8
            app.start(interactive=False)

        from rich.console import Console
        from rich.text import Text
        console = Console()

        try:
            while not self._stop.is_set():
                self._read_new_log()
                self._drain_events()
                snap = botstate.status_snapshot()
                width = console.width or 80
                renderable, keys = self._build(snap, width)

                # Redraw in place rather than scrolling: console.clear() over a
                # Live-ish loop keeps the panel anchored to the viewport.
                console.clear()
                console.print(renderable)
                console.print(Text.from_markup(keys))

                key = read_key(self.KEY_TIMEOUT)
                if key and not self._handle_key(key, console):
                    break
                time.sleep(self.RENDER_INTERVAL)
        except KeyboardInterrupt:
            # Same path as `q`: the finally below hands control back to the
            # caller, which stops the monitor and closes Chrome.
            pass
        finally:
            self._stop.set()
        return 0


# ──────────────────────────────────────────────────────────────────────────
# Entry point used by main.py
# ──────────────────────────────────────────────────────────────────────────

def run_panel(app, level: str = "important") -> int:
    """Run the interactive panel, or fall back to log streaming.

    Falls back when rich is missing or the terminal cannot render it, so a
    headless or minimal environment still gets a working (if plainer) bot.
    """
    try:
        import rich  # noqa: F401
    except ImportError:
        logger.warning("rich is not installed — falling back to plain log output. "
                       "Install it with: pip install rich")
        return run_plain(app)

    if not sys.stdout.isatty():
        # Piped or redirected: a full-screen panel would be meaningless noise
        # in a log file, so stream the log instead.
        logger.info("stdout is not a terminal — streaming logs instead of the panel")
        return run_plain(app)

    panel = TerminalPanel(app, level=level)
    return panel.run()


def run_plain(app) -> int:
    """The old behaviour: start the monitor and stream its log to the console."""
    if not app.is_running():
        app.start(interactive=False)
    try:
        while app.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0
