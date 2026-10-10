"""The Textual application: menu, screens, navigation and the shared bot binding.

`PonishaTUI` is a thin shell over the process-wide `BotApp`. It never builds its
own instance — `botstate.get_app()` is the only source, the same route the web
dashboard uses, because a second instance would carry its own Selenium driver
while the screens reported on the first.

The interface is English. The web dashboard is Persian because it is read by a
person browsing; this is read inside a terminal grid, where Persian is genuinely
fragile — the glyphs join, so the space a string occupies on screen does not
match its length, and columns stop lining up. Data coming from ponisha (badges,
error strings) stays Persian because it has to match the site.

Key handling is declarative (`BINDINGS`): Textual routes keys and tracks focus,
so there is no dispatch code here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from ponishabot.tui import bridge

logger = logging.getLogger(__name__)


class PonishaTUI(App):
    """Multi-screen terminal UI for ponishabot."""

    TITLE = "ponishabot"
    SUB_TITLE = "ponisha.ir monitor"

    # Palette taken from dashboard/static/dashboard/css/app.css so the terminal
    # and the web UI read as one product. The four surface levels are what give
    # the interface depth: cards sit on SURFACE_2 above a SURFACE_1 chrome,
    # with the page itself on BG.
    CSS = f"""
    Screen {{
        background: {bridge.BG};
        color: {bridge.FG};
    }}

    Header {{
        background: {bridge.SURFACE_1};
        color: {bridge.ACCENT};
        text-style: bold;
    }}
    Header .header--title {{ color: {bridge.ACCENT}; }}
    Header .header--subtitle {{ color: {bridge.MUTED_2}; }}

    Footer {{
        background: {bridge.SURFACE_1};
        color: {bridge.MUTED};
    }}
    Footer > .footer--key {{
        background: {bridge.ACCENT};
        color: {bridge.BG};
        text-style: bold;
    }}
    Footer > .footer--description {{ color: {bridge.MUTED_2}; }}

    .muted {{ color: {bridge.MUTED}; }}
    .dim {{ color: {bridge.MUTED_2}; }}
    .accent {{ color: {bridge.ACCENT}; }}
    .ok {{ color: {bridge.OK}; }}
    .danger {{ color: {bridge.DANGER}; }}
    .info {{ color: {bridge.INFO}; }}

    .card {{
        border: round {bridge.BORDER_STRONG};
        background: {bridge.SURFACE_2};
        padding: 1 2;
        margin: 0 1;
        height: auto;
    }}
    .card-title {{
        color: {bridge.MUTED_2};
        text-style: bold;
        padding: 0 0 1 0;
    }}

    /* The landing menu: centred and given room to breathe. */
    #menu {{ align: center middle; }}
    #menu-box {{
        border: round {bridge.ACCENT};
        background: {bridge.SURFACE_1};
        padding: 1 3;
        width: 58;
        height: auto;
    }}
    #menu-title {{
        color: {bridge.ACCENT};
        text-style: bold;
        padding: 0 0 1 0;
    }}
    #menu-sub {{ color: {bridge.MUTED_2}; padding: 0 0 1 0; }}
    .menu-item {{ color: {bridge.FG}; padding: 0 0 0 1; }}
    .menu-item-selected {{
        color: {bridge.BG};
        background: {bridge.ACCENT};
        text-style: bold;
        padding: 0 0 0 1;
    }}
    #menu-hint {{ color: {bridge.MUTED_2}; padding: 1 0 0 0; }}

    DataTable {{
        background: {bridge.SURFACE_2};
        border: round {bridge.BORDER_STRONG};
        margin: 0 1;
    }}
    DataTable > .datatable--header {{
        background: {bridge.SURFACE_3};
        color: {bridge.MUTED_2};
        text-style: bold;
    }}
    DataTable > .datatable--cursor {{
        background: {bridge.ACCENT};
        color: {bridge.BG};
        text-style: bold;
    }}
    Input, Select {{
        background: {bridge.SURFACE_2};
        border: tall {bridge.BORDER_STRONG};
        color: {bridge.FG};
    }}
    Input:focus, Select:focus {{ border: tall {bridge.ACCENT}; }}
    Input > .input--placeholder {{ color: {bridge.MUTED_2}; }}

    Switch:focus {{ border: tall {bridge.ACCENT}; }}
    Button {{ background: {bridge.SURFACE_3}; color: {bridge.FG}; }}
    Button.-primary {{ background: {bridge.ACCENT}; color: {bridge.BG}; }}

    VerticalScroll {{ background: {bridge.BG}; }}
    #notice {{ color: {bridge.MUTED}; padding: 0 2; height: auto; }}

    /* Settings: one tab per dashboard section, the active one in the accent. */
    #tabs {{ height: 1; padding: 0 1; background: {bridge.SURFACE_1}; }}
    .tab {{
        color: {bridge.MUTED};
        padding: 0 2;
        height: 1;
    }}
    .tab-active {{
        color: {bridge.BG};
        background: {bridge.ACCENT};
        text-style: bold;
        padding: 0 2;
        height: 1;
    }}
    .section {{
        padding: 1 2;
        height: auto;
    }}

    /* Label level with its field.
       The label is one row and its field is one row too, so both sat at the top
       of a three-row box and the text looked detached from the box it names.
       `align-vertical` is set on the *children*, not the row: the row's own
       value is not inherited, which is why centring it did nothing.
       `Switch` also hard-codes a content height of 1 in its DEFAULT_CSS, so it
       is sized here rather than left to report itself. */
    .field-row {{
        height: 3;
    }}
    .field-row > Label {{
        width: 28;
        height: 3;
        color: {bridge.MUTED};
        content-align: left middle;
    }}
    /* `min-height` rather than `height`: like Switch, an Input computes its own
       from the border, and that wins over an explicit height set here. */
    .field-row > Input {{
        min-height: 3;
    }}
    /* A Switch reports a content height of 1 through `get_content_height` and
       Textual honours that over an explicit height, so it cannot be stretched
       to three rows. Centring it in the row instead lines it up with the
       label, which is the thing that was actually wrong. */
    .field-row > Switch {{
        align-vertical: middle;
    }}

    .controls {{ height: auto; padding: 0 1 1 1; }}
    .controls Button {{ margin: 0 1 0 0; min-width: 12; }}
    """

    # Between screens: Tab/Shift+Tab, and `]`/`[` everywhere.
    #
    # The brackets exist because the Settings screen rebinds Tab to its section
    # tabs — a binding declared there is consulted before this one, so Tab could
    # no longer change screens. Rather than make the keys mean different things
    # per screen, `]`/`[` mean the same thing on all of them.
    #
    # No numeric shortcuts here on purpose: a digit bound at the app level is
    # swallowed by any focused Input before it arrives, so it would work on the
    # dashboard and silently fail on the login screen — the one screen where
    # typing a number is the whole point. The menu binds its own digits.
    BINDINGS = [
        Binding("q", "quit", "quit", priority=True),
        Binding("ctrl+c", "quit", "quit", show=False, priority=True),
        Binding("tab", "next_screen", "next", priority=True),
        Binding("shift+tab", "prev_screen", "prev", priority=True),
        # Screen changes while a text field has focus.
        #
        # `]`/`[` were the obvious choice and do not work: a focused Input
        # swallows any printable key before the app ever sees the event, so
        # those keys did nothing on the login and settings screens — both of
        # which hold focus in a field. Confirmed directly: a printable key
        # never reaches `App._on_key` at all. Ctrl+arrows are not printable and
        # arrive, so they are the ones bound.
        Binding("ctrl+right", "next_screen_always", "next", show=False,
                priority=True),
        Binding("ctrl+left", "prev_screen_always", "prev", show=False,
                priority=True),
        Binding("escape", "back", "back", priority=True),
        Binding("r", "refresh_status", "refresh", show=False),
        Binding("m", "menu", "menu", show=False),
    ]

    def compose(self) -> ComposeResult:
        """Chrome around every screen: a title bar and a key legend.

        The footer is not decoration. Without it nothing told the operator which
        keys exist — `s` starts and stops the bot, and the only way to discover
        that was to read the source. Textual builds the legend from BINDINGS, so
        a new binding shows up here on its own.
        """
        from textual.widgets import Footer, Header
        yield Header()
        yield Footer()

    SCREEN_CLASSES: list = []          # filled by the entry point

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

    def get_driver_class(self):
        """Return this platform's driver with mouse reporting switched off.

        Textual enables mouse reporting by default, which makes clicks, wheel
        scrolling, hover and drag-select all live. This interface is
        keyboard-only, and the driver is the only place to turn that off — every
        widget would otherwise have to swallow the events itself.

        The parent's choice is kept and only `mouse` is bound, so the Windows
        and Linux drivers are both covered without naming either.
        """
        import functools
        return functools.partial(super().get_driver_class(), mouse=False)

    # ── lifecycle ──
    def on_mount(self) -> None:
        for cls in self._installed_classes:
            self.install_screen(cls(self), name=cls.SCREEN_NAME)
        self.push_screen(self._installed_classes[0].SCREEN_NAME)

        # A TUI updates itself; nothing here blocks the event loop.
        self.set_interval(1.0, self._tick)
        self.set_interval(0.4, self._read_log)

        # Deliberately NOT starting the monitor. With dry_run off, launching
        # `main.py` used to begin bidding immediately, before the operator had
        # seen the settings or signed in. Starting is now an explicit choice
        # from the dashboard (`s`) or the menu.

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
                self.call_from_thread(self.notify, f"start failed: {e}")
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
    def notify(self, message: str, kind: str = "accent") -> None:
        """Show a transient line under the active screen."""
        colour = {"ok": bridge.OK, "err": bridge.DANGER,
                  "info": bridge.INFO}.get(kind, bridge.ACCENT)
        try:
            self.screen.query_one("#notice", Static).update(
                f"[{colour}]{message}[/]")
        except Exception:
            pass

    # Kept as an alias: the screens call `tui._notice(...)`.
    _notice = notify

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
        # Let a screen claim Tab if it has its own use for it.
        #
        # App._on_key checks the app's bindings first and only forwards the
        # event when they miss, so a screen binding for Tab never fires — the
        # app wins even against one marked priority. Delegating here is the only
        # way to make the key mean "next section" inside Settings while every
        # other screen keeps it as "next screen".
        if self._delegate_to_screen("next_section"):
            return
        self._cycle(1)

    def action_prev_screen(self) -> None:
        if self._delegate_to_screen("prev_section"):
            return
        self._cycle(-1)

    def action_next_screen_always(self) -> None:
        """Unconditional screen cycle, for `]`."""
        self._cycle(1)

    def action_prev_screen_always(self) -> None:
        """Unconditional screen cycle, for `[`."""
        self._cycle(-1)

    def _delegate_to_screen(self, action: str) -> bool:
        handler = getattr(self.screen, f"action_{action}", None)
        if handler is None:
            return False
        handler()
        return True

    def _cycle(self, step: int) -> None:
        names = [c.SCREEN_NAME for c in self._installed_classes]
        i = self.current_index()
        self.push_screen(names[(i + step) % len(names)])

    def action_menu(self) -> None:
        """`m`: back to the landing menu from anywhere."""
        self.push_screen(self._installed_classes[0].SCREEN_NAME)

    def action_back(self) -> None:
        """Esc: leave a modal/edit, else return to the menu."""
        if len(self.screen_stack) > 2:
            self.pop_screen()
            return
        if self.current_index() != 0:
            self.push_screen(self._installed_classes[0].SCREEN_NAME)

    def action_refresh_status(self) -> None:
        self._tick()
        self.notify("refreshed", "info")
