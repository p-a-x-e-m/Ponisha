"""The Textual application: screens, navigation and the shared bot binding.

`PonishaTUI` is a thin shell over the process-wide `BotApp`. It never builds its
own instance — `botstate.get_app()` is the only source, the same route the web
dashboard uses, because a second instance would carry its own Selenium driver
while the screens reported on the first.

Key handling is declarative (`BINDINGS`), which is what makes this a TUI rather
than a panel that happens to redraw: Textual routes the keys, tracks focus, and
gives each screen its own bindings without any dispatch code here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static

from ponishabot.tui import bridge

logger = logging.getLogger(__name__)


class PonishaTUI(App):
    """Multi-screen terminal UI for ponishabot."""

    TITLE = "ponishabot"
    SUB_TITLE = "monitor for ponisha.ir"

    CSS = f"""
    Screen {{ background: #060809; }}
    Header {{ background: #0b0e11; color: {bridge.FG}; }}
    Footer {{ background: #0b0e11; color: {bridge.MUTED}; }}
    Footer > .footer--key {{ background: {bridge.ACCENT}; color: #060809; }}
    .muted {{ color: {bridge.MUTED}; }}
    .accent {{ color: {bridge.ACCENT}; }}
    .ok {{ color: {bridge.OK}; }}
    .danger {{ color: {bridge.DANGER}; }}
    .card {{
        border: round {bridge.BORDER};
        background: #0b0e11;
        padding: 0 1;
        margin: 0 1;
    }}
    DataTable {{ background: #0b0e11; }}
    DataTable > .datatable--cursor {{ background: {bridge.ACCENT}; color: #060809; }}
    Input, Select {{ background: #0b0e11; border: tall {bridge.BORDER}; }}
    Input:focus, Select:focus {{ border: tall {bridge.ACCENT}; }}
    """

    # Navigation is on Tab/Shift+Tab and the arrow keys plus hjkl, per the
    # documented behaviour. Screens add their own bindings on top.
    BINDINGS = [
        Binding("q", "quit", "خروج", priority=True),
        Binding("ctrl+c", "quit", "خروج", show=False, priority=True),
        Binding("tab", "next_screen", "صفحه بعد", priority=True),
        Binding("shift+tab", "prev_screen", "صفحه قبل", priority=True),
        # Written without the `self.` prefix so Textual passes the argument
        # through; as an unbound name it looks for a method literally called
        # `goto(0)` and silently falls back to the first binding.
        # No single-key screen shortcuts on purpose. A digit bound at the app
        # level is swallowed by any focused Input before it arrives, so the
        # shortcut would work on the dashboard and silently fail on the login
        # screen — the one screen where typing a number is the whole point.
        # Tab and Shift+Tab always reach the app, so they are the only way
        # between screens.
        Binding("escape", "back", "برگشت", priority=True),
        Binding("r", "refresh_status", "بازخوانی", show=False),
    ]

    # Screen order drives Tab and the number keys.
    SCREEN_CLASSES: list = []          # filled after the screens are imported

    def __init__(self, app, level: str = "important"):
        super().__init__()
        self.bot = app                # the shared BotApp singleton
        self.log_level = level
        self.show_noise = False
        self._started_at: Optional[float] = None
        self._log_offset = 0
        self._log_buf: list = []
        self._lock = threading.Lock()
        self._installed_classes = list(self.SCREEN_CLASSES)

    # ── lifecycle ──
    def on_mount(self) -> None:
        for cls in self._installed_classes:
            self.install_screen(cls(self), name=cls.SCREEN_NAME)
        self.push_screen(self._installed_classes[0].SCREEN_NAME)

        # A TUI updates itself; nothing here blocks the event loop.
        self.set_interval(1.0, self._tick)
        self.set_interval(0.4, self._read_log)

        # The monitor runs off the main thread, as it does for the dashboard.
        if not self.bot.is_running():
            self._notice("در حال شروع مانیتور…")
            self.start_monitor()

    def on_unmount(self) -> None:
        # Leave the bot in a known state rather than a half-open Chrome.
        try:
            if self.bot.is_running():
                self.bot.stop()
        except Exception as e:
            logger.warning(f"stop on exit failed: {type(e).__name__}: {e}")

    # ── bot control ──
    def start_monitor(self) -> None:
        def worker():
            try:
                self.bot.start(interactive=False)
            except Exception as e:
                logger.error(f"start failed: {type(e).__name__}: {e}")
                self.call_from_thread(self._notice, f"شروع نشد: {e}")
        threading.Thread(target=worker, daemon=True, name="tui-start").start()

    def stop_monitor(self) -> None:
        def worker():
            try:
                self.bot.stop()
            except Exception as e:
                logger.error(f"stop failed: {type(e).__name__}: {e}")
        threading.Thread(target=worker, daemon=True, name="tui-stop").start()

    # ── state for the screens ──
    def snapshot(self) -> dict:
        from ponishabot import botstate
        try:
            snap = dict(botstate.status_snapshot())
        except Exception as e:
            logger.debug(f"status_snapshot failed: {e}")
            snap = {}
        snap["uptime"] = (time.time() - self._started_at) if self._started_at else None
        snap["dry_run"] = bool(getattr(self.bot.cfg, "dry_run", True))
        return snap

    def recent_projects(self) -> list:
        from ponishabot import botstate
        try:
            return botstate.get_projects()
        except Exception:
            return []

    def log_lines(self) -> list:
        with self._lock:
            return list(self._log_buf)

    def clear_log(self) -> None:
        with self._lock:
            self._log_buf.clear()

    # ── notice ──
    def _notice(self, message: str) -> None:
        """Show a transient line in the status bar area."""
        try:
            bar = self.query_one("#notice", Static)
            bar.update(f"[{bridge.ACCENT}]{message}[/]")
        except Exception:
            pass

    # ── intervals ──
    def _tick(self) -> None:
        """Refresh the visible screen's status widgets."""
        try:
            screen = self.screen
            if hasattr(screen, "refresh_status"):
                screen.refresh_status()
        except Exception:
            pass
        if self.bot.is_running() and self._started_at is None:
            self._started_at = time.time()
        elif not self.bot.is_running():
            self._started_at = None

    def _read_log(self) -> None:
        from ponishabot import botstate
        try:
            chunk = botstate.read_log_tail(since_byte=self._log_offset)
        except Exception:
            return
        self._log_offset = chunk.get("since", self._log_offset)
        text = chunk.get("text") or ""
        if not text:
            return
        with self._lock:
            self._log_buf.extend(l for l in text.splitlines() if l.strip())
            if len(self._log_buf) > 4000:
                del self._log_buf[:-2000]

    # ── navigation actions ──
    def current_index(self) -> int:
        """Position of the visible screen in the installed order, or 0.

        Keyed on each class's own SCREEN_NAME rather than `screen.name`:
        `Screen.name` is a constructor argument that defaults to None, so every
        screen would otherwise compare equal and Tab would never advance.
        """
        current = type(self.screen)
        for i, cls in enumerate(self._installed_classes):
            if current is cls:
                return i
        return 0

    def action_next_screen(self) -> None:
        self._cycle(1)

    def action_prev_screen(self) -> None:
        self._cycle(-1)

    def _cycle(self, step: int) -> None:
        names = [c.SCREEN_NAME for c in self._installed_classes]
        i = self.current_index()
        self.push_screen(names[(i + step) % len(names)])

    def action_back(self) -> None:
        """Esc: leave a modal/edit, else go back to the dashboard."""
        if len(self.screen_stack) > 2:
            self.pop_screen()
            return
        if self.current_index() != 0:
            self.push_screen(self._installed_classes[0].SCREEN_NAME)

    def action_refresh_status(self) -> None:
        self._tick()
        self._notice("بازخوانی شد")
