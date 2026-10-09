"""The screens: menu, dashboard, projects, settings, login, log.

Each screen owns its widgets and its own key bindings. Navigation (Tab, arrows,
hjkl, Enter, Esc) is declared per screen and routed by Textual, so there is no
key-dispatch code to maintain.

The interface is English. Data from ponisha stays as the site sends it — badges
like «فوری» and the API's own error strings are matched, not displayed as
literals we chose.
"""

from __future__ import annotations

import logging
import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Middle, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Label, Static, Switch

from ponishabot.tui import bridge

logger = logging.getLogger(__name__)


class BaseScreen(Screen):
    """Shared chrome and the movement keys every list screen gets."""

    SCREEN_NAME = "base"
    NAV = "Base"

    BINDINGS = [
        # hjkl alongside the arrow keys, as the README describes.
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
    ]

    def __init__(self, tui) -> None:
        super().__init__()
        self.tui = tui
        self.bot = tui.bot

    # Default no-ops so a screen without lists still answers the shared keys.
    def action_cursor_down(self) -> None:
        pass

    def action_cursor_up(self) -> None:
        pass


class MenuScreen(BaseScreen):
    """The landing screen. Pick a screen by number, or with arrows and Enter.

    Nothing starts on its own here. With dry_run off, launching `main.py` used
    to begin bidding before the operator had looked at anything; choosing each
    step explicitly is the point of this screen.
    """

    SCREEN_NAME = "menu"
    NAV = "Menu"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("1", "open(0)", "Dashboard", show=False, priority=True),
        Binding("2", "open(1)", "Projects", show=False, priority=True),
        Binding("3", "open(2)", "Settings", show=False, priority=True),
        Binding("4", "open(3)", "Login", show=False, priority=True),
        Binding("5", "open(4)", "Log", show=False, priority=True),
        Binding("enter", "select", "open"),
    ]

    ITEMS = [
        ("Dashboard", "live status and controls"),
        ("Projects", "scanned listings"),
        ("Settings", "bot configuration"),
        ("Login", "sign in to ponisha"),
        ("Log", "live output"),
    ]

    def __init__(self, tui) -> None:
        super().__init__(tui)
        # Set here, not in on_mount: compose() runs before on_mount, and it
        # reads the cursor to decide which row to highlight.
        self._cursor = 0

    def compose(self) -> ComposeResult:
        with Center(), Middle():
            with Vertical(id="menu-box"):
                yield Static("ponishabot", id="menu-title")
                yield Static("ponisha.ir monitor", id="menu-sub")
                for i, (name, desc) in enumerate(self.ITEMS, start=1):
                    yield Static(self._row(i, name, desc, i - 1 == self._cursor),
                                 classes="menu-item", id=f"menu-{i}")
                # Built as Text: a bare `[q]` is markup for an unknown style,
                # and Rich renders nothing for it.
                from rich.text import Text as _Text
                quit_row = _Text()
                quit_row.append("  ", style=bridge.MUTED)
                quit_row.append("[q]", style=bridge.ACCENT)
                quit_row.append("  quit", style=bridge.MUTED)
                yield Static(quit_row, classes="menu-item")
                yield Static("arrows move · Enter opens · or press the number",
                             id="menu-hint")

    def on_mount(self) -> None:
        self._redraw()

    def _row(self, i: int, name: str, desc: str, selected: bool):
        """Build one menu row as a `Text`, not markup.

        `[1]` is valid Rich markup naming an unknown style, and Rich renders
        nothing at all for it — the menu opened with every shortcut number
        missing. Backslash-escaping does not help either: markup only
        unescapes `\\\\[`, so `\\[1\\]` came out literally as `[1\\]`. Building
        the row from styled spans sidesteps markup entirely.
        """
        from rich.text import Text

        row = Text()
        if selected:
            row.append("▸ ", style=f"bold {bridge.BG} on {bridge.ACCENT}")
            row.append(f"[{i}]", style=f"bold {bridge.BG} on {bridge.ACCENT}")
            row.append(f"  {name:<10} ", style=f"bold {bridge.BG} on {bridge.ACCENT}")
            row.append(desc, style=f"{bridge.BG} on {bridge.ACCENT}")
        else:
            row.append("  ", style=bridge.MUTED)
            row.append(f"[{i}]", style=bridge.ACCENT)
            row.append(f"  {name:<10} ", style=bridge.FG)
            row.append(desc, style=bridge.MUTED_2)
        return row

    def _redraw(self) -> None:
        for i, (name, desc) in enumerate(self.ITEMS, start=1):
            try:
                self.query_one(f"#menu-{i}", Static).update(
                    self._row(i, name, desc, i - 1 == self._cursor))
            except Exception:
                pass

    def refresh_status(self) -> None:
        self._redraw()

    # ── movement ──
    def action_cursor_down(self) -> None:
        self._cursor = min(self._cursor + 1, len(self.ITEMS) - 1)
        self._redraw()

    def action_cursor_up(self) -> None:
        self._cursor = max(self._cursor - 1, 0)
        self._redraw()

    def action_select(self) -> None:
        """Enter opens the highlighted item."""
        self._open(self._cursor)

    def action_open(self, index) -> None:
        """Number key: Textual passes binding arguments through as text."""
        try:
            i = int(index)
        except (TypeError, ValueError):
            return
        self._open(i)

    def _open(self, index: int) -> None:
        classes = self.tui._installed_classes
        # +1 skips the menu itself, which is first in the installed order.
        target = index + 1
        if 0 <= index < len(self.ITEMS) and target < len(classes):
            self.app.push_screen(classes[target].SCREEN_NAME)


class DashboardScreen(BaseScreen):
    """Live state and the controls that change it."""

    SCREEN_NAME = "dashboard"
    NAV = "Dashboard"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("s", "toggle_run", "start/stop"),
        Binding("d", "toggle_dry_run", "dry-run"),
        Binding("l", "login", "login", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Horizontal(Static("", id="status-card", classes="card"),
                         Static("", id="meta-card", classes="card"))
        yield Static("Recent proposals", classes="card-title")
        yield DataTable(id="recent")
        yield Static("", id="notice")

    def on_mount(self) -> None:
        table = self.query_one("#recent", DataTable)
        table.add_columns("price", "title", "days")
        table.cursor_type = "row"
        self.refresh_status()

    def refresh_status(self) -> None:
        snap = self.tui.snapshot()
        rows = dict(bridge.status_rows(snap))

        state = rows.get("status", "?")
        colour = {"RUNNING": bridge.OK, "BIDDING": bridge.INFO}.get(
            state, bridge.MUTED)
        self.query_one("#status-card", Static).update(
            f"[b {colour}]{state}[/]\n"
            f"quota [{bridge.ACCENT}]{rows.get('quota')}[/]   "
            f"matches {rows.get('matches')}   bids {rows.get('bids')}")

        dry = snap.get("dry_run")
        self.query_one("#meta-card", Static).update(
            f"uptime {rows.get('uptime')}\n"
            f"ai {rows.get('ai')}   poll {rows.get('poll')}   "
            f"session {rows.get('session')}\n"
            f"dry-run [{bridge.DANGER if dry else bridge.MUTED}]"
            f"{'on' if dry else 'off'}[/]")

        table = self.query_one("#recent", DataTable)
        wanted = self.tui.recent_projects()[:20]
        if table.row_count != len(wanted):
            table.clear()
            for p in wanted:
                days = str(p.get("suggested_days") or "—")
                table.add_row(bridge.fmt_money(p.get("price")),
                              str(p.get("title", ""))[:48], days)

    # ── actions ──
    def action_cursor_down(self) -> None:
        self.query_one("#recent", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#recent", DataTable).action_cursor_up()

    def action_toggle_run(self) -> None:
        if self.bot.is_running():
            self.tui.notify("stopping…", "info")
            self.tui.stop_monitor()
        else:
            if self.bot.monitor is None:
                self.tui.notify("not signed in — open Login first", "err")
                return
            self.tui.notify("starting…", "ok")
            self.tui.start_monitor()

    def action_toggle_dry_run(self) -> None:
        new = not self.bot.cfg.dry_run
        self.bot.set_dry_run(new)
        self.tui.notify(f"dry-run {'on' if new else 'off'}", "ok")
        self.refresh_status()

    def action_login(self) -> None:
        if self.bot.monitor is None:
            self.tui.notify("not signed in — open Login first", "err")
            return
        threading.Thread(target=self.bot.open_login_browser, daemon=True).start()
        self.tui.notify("opening the login window…", "info")


class ProjectsScreen(BaseScreen):
    """Every scanned project, with the decision detail behind Enter."""

    SCREEN_NAME = "projects"
    NAV = "Projects"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("enter", "open_detail", "details"),
        Binding("b", "bid", "bid"),
    ]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search…", id="search")
        yield DataTable(id="projects")
        yield VerticalScroll(Static("", id="detail", classes="card"))

    def on_mount(self) -> None:
        t = self.query_one("#projects", DataTable)
        t.add_columns("price", "title", "rivals", "reason")
        t.cursor_type = "row"
        self._rows: list = []
        self.refresh_status()
        # Focus the table, not the search box: with the box focused every digit
        # went into it, so the shortcut keys never fired. `/` moves focus into
        # the box when you actually want to type.
        t.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_key(self, event) -> None:
        """`/` focuses search; Esc inside it gives the keys back."""
        if event.key == "slash":
            self.query_one("#search", Input).focus()
            event.stop()
        elif event.key == "escape" and isinstance(self.focused, Input):
            self.query_one("#projects", DataTable).focus()
            event.stop()

    def refresh_status(self) -> None:
        self._fill(self.query_one("#search", Input).value)

    def _fill(self, needle: str) -> None:
        needle = (needle or "").strip().lower()
        rows = [p for p in self.tui.recent_projects()
                if not needle or needle in str(p.get("title", "")).lower()]
        self._rows = rows
        t = self.query_one("#projects", DataTable)
        t.clear()
        for p in rows:
            t.add_row(bridge.fmt_money(p.get("price")),
                      str(p.get("title", ""))[:52],
                      str(p.get("bid_count", "") or "—"),
                      str(p.get("reason", "") or "—")[:30])

    def action_cursor_down(self) -> None:
        self.query_one("#projects", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#projects", DataTable).action_cursor_up()

    def _selected(self):
        t = self.query_one("#projects", DataTable)
        if not self._rows or t.cursor_row >= len(self._rows):
            return None
        return self._rows[t.cursor_row]

    def action_open_detail(self) -> None:
        """Enter: reveal the decision detail for the highlighted row."""
        p = self._selected()
        if p is None:
            self.tui.notify("no project selected", "err")
            return
        detail = (
            f"[b {bridge.ACCENT}]{p.get('title', '')}[/]\n"
            f"price {bridge.fmt_money(p.get('price'))}   "
            f"budget {bridge.fmt_money(p.get('budget_min'))}"
            f"–{bridge.fmt_money(p.get('budget_max'))}\n"
            f"rivals {p.get('bid_count', '?')}   "
            f"days {p.get('suggested_days', '?')}\n"
            f"reason [{bridge.MUTED}]{p.get('reason', '—')}[/]\n"
            f"[{bridge.MUTED_2}]{str(p.get('url', ''))}[/]")
        self.query_one("#detail", Static).update(detail)

    def action_bid(self) -> None:
        """b: place a manual bid on the highlighted project."""
        if self.bot.cfg.dry_run:
            self.tui.notify("dry-run is on — bidding is disabled", "err")
            return
        p = self._selected()
        if p is None:
            self.tui.notify("no project selected", "err")
            return
        from ponishabot.models import Project
        obj = Project(**{k: p.get(k) for k in Project.__dataclass_fields__})
        self.tui.notify(f"bidding on {str(p.get('title'))[:30]}…", "info")
        threading.Thread(target=self.bot.manual_bid, args=(obj,),
                         daemon=True, name="tui-bid").start()


class SettingsScreen(BaseScreen):
    """Every setting the dashboard exposes, edit-and-save in place.

    Values load from `app.cfg` on entry and write back through the same
    `BotApp.set_*` methods the dashboard uses, so `config.yaml` stays the single
    source of truth and a setting survives a restart.
    """

    SCREEN_NAME = "settings"
    NAV = "Settings"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("ctrl+s", "save", "save"),
        Binding("enter", "focus_next", "next"),
        Binding("escape", "revert", "revert"),
        Binding("x", "toggle_switch", "toggle", show=False),
    ]

    # (config attribute, label, kind) — mirrors the dashboard's sections.
    FIELDS = [
        ("dry_run", "notify only (dry_run)", "switch"),
        ("headless", "headless Chrome", "switch"),
        ("show_browser", "show live browser", "switch"),
        ("refresh_interval", "poll interval (sec)", "int"),
        ("max_pages", "max pages", "int"),
        ("priority_window_minutes", "priority window (min)", "int"),
        ("min_skill_match", "min skill match", "float"),
        ("min_remaining_proposals", "stop at quota", "int"),
        ("budget_min", "budget min (Toman)", "int"),
        ("budget_max", "budget max (Toman)", "int"),
        ("telegram_token", "Telegram token", "secret"),
        ("telegram_chat_id", "Telegram chat id", "text"),
        ("skills", "skills (comma separated)", "list"),
        ("profile_name", "name", "text"),
        ("profile_about", "AI instructions", "text"),
        ("profile_hourly_min", "hourly min", "int"),
        ("profile_hourly_max", "hourly max", "int"),
        ("ai_enabled", "AI engine", "switch"),
        ("ai_base_url", "endpoint URL", "text"),
        ("ai_api_key", "API key", "secret"),
        ("ai_model", "model", "text"),
        ("ai_min_skill_gate", "skill gate", "float"),
        ("ai_temperature", "temperature", "float"),
        ("ai_max_description_chars", "max description chars", "int"),
        ("karlancer_enabled", "Karlancer market data", "switch"),
        ("karlancer_max_pages", "Karlancer pages", "int"),
        ("high_competition_threshold", "high competition at", "int"),
        ("urgency_premium_percent", "urgent premium (%)", "int"),
        ("premium_badge_multiplier", "premium badge multiplier", "float"),
        ("low_competition_discount_percent", "low competition (%)", "int"),
        ("high_competition_discount_percent", "high competition (%)", "int"),
        ("forex_enabled", "forex uplift", "switch"),
        ("baseline_usd_rate", "baseline USD rate", "int"),
        ("forex_sensitivity", "forex sensitivity", "float"),
        ("forex_source", "rate source", "text"),
        ("karlancer_soft_clamp_enabled", "soft clamp", "switch"),
        ("karlancer_clamp_multiplier", "clamp multiplier", "float"),
        ("karlancer_target_multiplier", "target multiplier", "float"),
    ]

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="fields")
        yield Static("", id="notice")

    def on_mount(self) -> None:
        box = self.query_one("#fields", VerticalScroll)
        self._inputs: dict = {}
        for attr, label, kind in self.FIELDS:
            value = getattr(self.bot.cfg, attr, "")
            if kind == "switch":
                box.mount(Horizontal(
                    Label(f"{label}  ", classes="muted"),
                    Switch(value=bool(value), id=f"f-{attr}"), classes="row"))
            else:
                if kind == "secret":
                    value = "•" * 8 if value else ""
                elif kind == "list":
                    value = ", ".join(value or [])
                inp = Input(value=str(value), password=(kind == "secret"),
                            id=f"f-{attr}")
                self._inputs[attr] = inp
                box.mount(Horizontal(Label(f"{label}  ", classes="muted"), inp,
                                     classes="row"))
        self._note("Tab between fields · Ctrl+S to save · Esc to revert")

    def _note(self, message: str, kind: str = "muted") -> None:
        colour = {"ok": bridge.OK, "err": bridge.DANGER}.get(kind, bridge.MUTED_2)
        self.query_one("#notice", Static).update(f"[{colour}]{message}[/]")

    def action_toggle_switch(self) -> None:
        """x flips the switch the cursor is on."""
        focused = self.focused
        if isinstance(focused, Switch):
            focused.value = not focused.value

    def action_revert(self) -> None:
        """Esc: reload from config, discarding unsaved edits."""
        for attr, _label, kind in self.FIELDS:
            if kind == "switch":
                continue
            value = getattr(self.bot.cfg, attr, "")
            if kind == "list":
                value = ", ".join(value or [])
            elif kind == "secret":
                value = "•" * 8 if value else ""
            inp = self._inputs.get(attr)
            if inp is not None:
                inp.value = str(value)
        self._note("unsaved changes reverted")

    def action_save(self) -> None:
        """Ctrl+S: validate every field, then hand it to the BotApp setters."""
        form = {}
        for attr, _label, kind in self.FIELDS:
            if kind == "switch":
                form[attr] = bool(self.query_one(f"#f-{attr}", Switch).value)
            else:
                inp = self._inputs.get(attr)
                if inp is None:
                    continue
                raw = inp.value
                if kind == "secret" and set(raw) == {"•"}:
                    continue          # untouched placeholder: keep the stored key
                form[attr] = raw

        try:
            values = bridge.settings_from_form(form, self.bot.cfg)
        except bridge.InvalidSetting as e:
            self._note(f"not saved: {e}", "err")
            return

        cfg = self.bot.cfg
        # Route each value through the BotApp setter that owns it, so a live
        # monitor sees the change rather than only the file changing.
        self.bot.set_dry_run(values.get("dry_run", cfg.dry_run))
        self.bot.cfg.headless = values.get("headless", cfg.headless)
        self.bot.cfg.show_browser = values.get("show_browser", cfg.show_browser)
        self.bot.set_refresh_interval(values.get("refresh_interval", cfg.refresh_interval))
        cfg.max_pages = values.get("max_pages", cfg.max_pages)
        cfg.priority_window_minutes = values.get("priority_window_minutes",
                                                 cfg.priority_window_minutes)
        cfg.min_skill_match = values.get("min_skill_match", cfg.min_skill_match)
        cfg.min_remaining_proposals = values.get("min_remaining_proposals",
                                                 cfg.min_remaining_proposals)
        self.bot.set_telegram(values.get("telegram_token", cfg.telegram_token),
                              values.get("telegram_chat_id", cfg.telegram_chat_id))
        self.bot.set_profile(
            skills=values.get("skills", cfg.search_skills),
            min_budget=values.get("budget_min", cfg.budget_min),
            max_budget=values.get("budget_max", cfg.budget_max),
            name=values.get("profile_name", cfg.profile_name),
            about=values.get("profile_about", cfg.profile_about),
            hourly_min=values.get("profile_hourly_min", cfg.profile_hourly_min),
            hourly_max=values.get("profile_hourly_max", cfg.profile_hourly_max))
        self.bot.set_ai_config(
            enabled=values.get("ai_enabled", cfg.ai_enabled),
            api_key=values.get("ai_api_key", cfg.ai_api_key),
            model=values.get("ai_model", cfg.ai_model),
            base_url=values.get("ai_base_url", cfg.ai_base_url))
        for attr in ("ai_min_skill_gate", "ai_temperature", "ai_max_description_chars",
                     "karlancer_enabled", "karlancer_max_pages",
                     "high_competition_threshold", "urgency_premium_percent",
                     "premium_badge_multiplier", "low_competition_discount_percent",
                     "high_competition_discount_percent", "forex_enabled",
                     "baseline_usd_rate", "forex_sensitivity", "forex_source",
                     "karlancer_soft_clamp_enabled", "karlancer_clamp_multiplier",
                     "karlancer_target_multiplier"):
            if attr in values:
                setattr(cfg, attr, values[attr])

        from ponishabot.config import ConfigLoader
        ConfigLoader.save(cfg)
        self._note("saved ✓", "ok")


class LoginScreen(BaseScreen):
    """Sign in without leaving the terminal: OTP, or hand off to Chrome."""

    SCREEN_NAME = "login"
    NAV = "Login"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("enter", "submit", "submit"),
        Binding("o", "browser_login", "browser"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="session", classes="card")
        yield Horizontal(Label("mobile  ", classes="muted"),
                         Input(placeholder="09121234567", id="mobile"))
        yield Horizontal(Label("code    ", classes="muted"),
                         Input(placeholder="------", id="otp"), id="otp-row")
        yield Static("", id="note", classes="card")
        yield Horizontal(Button("Send code", id="btn-request", variant="primary"),
                         Button("Sign in with browser", id="btn-browser"))

    def on_mount(self) -> None:
        self.query_one("#otp-row").display = False
        self._mobile = ""
        self.refresh_status()

    def refresh_status(self) -> None:
        snap = self.tui.snapshot()
        ok = snap.get("has_cookies")
        self.query_one("#session", Static).update(
            f"session: [b {bridge.OK if ok else bridge.DANGER}]"
            f"{'valid' if ok else 'invalid — sign in below'}[/]\n"
            f"[{bridge.MUTED_2}]OTP needs no browser. "
            f"\"Sign in with browser\" opens Chrome.[/]")

    def _note(self, message: str, kind: str = "muted") -> None:
        colour = {"ok": bridge.OK, "err": bridge.DANGER}.get(kind, bridge.MUTED)
        self.query_one("#note", Static).update(f"[{colour}]{message}[/]")

    def action_submit(self) -> None:
        """Enter: send the code, or verify it if the code field is showing."""
        from ponishabot.auth import PonishaAuth
        mobile = self.query_one("#mobile", Input).value
        otp = self.query_one("#otp", Input).value

        if mobile and not self._mobile:
            if not PonishaAuth.normalize_mobile(mobile):
                self._note("invalid mobile number (e.g. 09121234567)", "err")
                return
            self._mobile = mobile
            self._note("sending code…")
            threading.Thread(target=self._request, args=(mobile,),
                             daemon=True).start()
        elif self._mobile and otp:
            self._note("verifying…")
            threading.Thread(target=self._verify, args=(self._mobile, otp),
                             daemon=True).start()
        else:
            self._note("enter a number, then the SMS code", "err")

    def _request(self, mobile: str) -> None:
        result = self.bot.auth.request_otp(mobile)
        if result.get("ok"):
            self.call_from_thread(self._show_otp_row)
        else:
            # The error text comes from ponisha's own API and stays as sent.
            self.call_from_thread(self._note,
                                  result.get("error", "could not send the code"),
                                  "err")

    def _show_otp_row(self) -> None:
        self.query_one("#otp-row").display = True
        self.query_one("#otp", Input).focus()
        self._note("enter the SMS code and press Enter")

    def _verify(self, mobile: str, otp: str) -> None:
        result = self.bot.auth.verify_otp(mobile, otp)
        if result.get("ok"):
            self._mobile = ""
            self.call_from_thread(self._note, "signed in ✓", "ok")
            self.call_from_thread(self.query_one("#otp-row").__setattr__,
                                  "display", False)
            self.call_from_thread(self.refresh_status)
        else:
            self.call_from_thread(self._note,
                                  result.get("error", "invalid code"), "err")

    def action_browser_login(self) -> None:
        """o / button: the Chrome path, for when OTP is not set up."""
        if self.bot.monitor is None:
            self._note("start the bot from the Dashboard first", "err")
            return
        threading.Thread(target=self.bot.open_login_browser,
                         daemon=True).start()
        self._note("Chrome is opening — sign in, then close the window")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-request":
            self.action_submit()
        elif event.button.id == "btn-browser":
            self.action_browser_login()


class LogScreen(BaseScreen):
    """The log, filtered so a healthy run is readable."""

    SCREEN_NAME = "log"
    NAV = "Log"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("f", "cycle_level", "level"),
        Binding("n", "toggle_noise", "noise"),
        Binding("c", "clear", "clear"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="log-head", classes="card")
        yield VerticalScroll(Static("", id="log-body"))

    def on_mount(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        lines = bridge.filter_lines(self.tui.log_lines(),
                                    level=self.tui.log_level,
                                    show_noise=self.tui.show_noise)
        collapsed = bridge.collapse_repeats(lines)[-200:]
        # The noise state sits inside the colour tag, not inside square
        # brackets of its own: `[hidden]` is valid Rich markup syntax for an
        # unknown style, and Rich silently renders nothing for it — the label
        # vanished from the header. Only the level name is safe in brackets
        # because the three values are real style names, and even that is
        # incidental.
        noise = "shown" if self.tui.show_noise else "hidden"
        self.query_one("#log-head", Static).update(
            f"level [{bridge.ACCENT}]{self.tui.log_level}[/]   "
            f"noise [{bridge.MUTED}]{noise}[/]   "
            f"rows {len(collapsed)}\n"
            f"[{bridge.MUTED_2}]f level · n noise · c clear[/]")

        from rich.text import Text
        body = Text()
        for text, count in collapsed:
            room = max(20, self.size.width - 8)
            body.append(bridge.truncate_width(text, room) + "\n",
                        style=bridge.MUTED)
            if count > 1:
                body.append(f"  ×{count}\n", style="dim")
        self.query_one("#log-body", Static).update(body)

    def action_cycle_level(self) -> None:
        i = bridge.LEVELS.index(self.tui.log_level)
        self.tui.log_level = bridge.LEVELS[(i + 1) % len(bridge.LEVELS)]
        self.refresh_status()

    def action_toggle_noise(self) -> None:
        self.tui.show_noise = not self.tui.show_noise
        self.refresh_status()

    def action_clear(self) -> None:
        self.tui.clear_log()
        self.refresh_status()


# Menu first: Tab starts and Esc returns here.
ALL_SCREENS = [MenuScreen, DashboardScreen, ProjectsScreen,
               SettingsScreen, LoginScreen, LogScreen]
