"""The screens: dashboard, projects, settings, login, log.

Each screen owns its widgets and its own key bindings. Navigation (Tab, arrows,
hjkl, Enter, Esc) is declared per screen and routed by Textual, so there is no
key-dispatch code to maintain — that is the difference between this and a panel
that merely redraws.
"""

from __future__ import annotations

import logging
import threading
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (Button, DataTable, Footer, Input, Label, Select,
                             Static, Switch)

from ponishabot.tui import bridge

logger = logging.getLogger(__name__)


class BaseScreen(Screen):
    """Shared chrome and the vim-style movement keys every list screen gets."""

    SCREEN_NAME = "base"
    NAV = "داشبورد"

    BINDINGS = [
        # hjkl alongside the arrow keys, as the README describes.
        Binding("j", "cursor_down", "پایین", show=False),
        Binding("k", "cursor_up", "بالا", show=False),
        Binding("down", "cursor_down", "پایین", show=False),
        Binding("up", "cursor_up", "بالا", show=False),
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


class DashboardScreen(BaseScreen):
    """Live state, the controls, and the last few proposals."""

    SCREEN_NAME = "dashboard"
    NAV = "داشبورد"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("s", "toggle_run", "شروع/توقف"),
        Binding("d", "toggle_dry_run", "dry-run"),
        Binding("l", "login", "ورود", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Horizontal(Static("", id="status-card", classes="card"),
                         Static("", id="meta-card", classes="card"))
        yield Static("آخرین پیشنهادها", classes="muted")
        yield DataTable(id="recent")
        yield Static("", id="notice")

    def on_mount(self) -> None:
        table = self.query_one("#recent", DataTable)
        table.add_columns("قیمت", "عنوان", "مدت")
        table.cursor_type = "row"
        self.refresh_status()

    def refresh_status(self) -> None:
        snap = self.tui.snapshot()
        rows = dict(bridge.status_rows(snap))

        state = rows.get("وضعیت", "?")
        colour = bridge.OK if snap.get("running") else bridge.MUTED
        self.query_one("#status-card", Static).update(
            f"[b {colour}]{state}[/]\n"
            f"سهمیه [{bridge.ACCENT}]{rows.get('سهمیه')}[/]   "
            f"متچ {rows.get('متچ')}   بید {rows.get('بید')}")

        self.query_one("#meta-card", Static).update(
            f"Uptime {rows.get('Uptime')}\n"
            f"AI {rows.get('AI')}   فاصله {rows.get('فاصله')}   "
            f"جلسه {rows.get('جلسه')}\n"
            f"dry-run "
            f"[{bridge.DANGER if snap.get('dry_run') else bridge.MUTED}]"
            f"{'روشن' if snap.get('dry_run') else 'خاموش'}[/]")

        table = self.query_one("#recent", DataTable)
        wanted = self.tui.recent_projects()[:20]
        if table.row_count != len(wanted):
            table.clear()
            for p in wanted:
                dur = f"{p.get('suggested_days')} روز" if p.get("suggested_days") else "—"
                table.add_row(bridge.fmt_money(p.get("price")),
                              str(p.get("title", ""))[:48], dur)

    # ── actions ──
    def action_cursor_down(self) -> None:
        self.query_one("#recent", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#recent", DataTable).action_cursor_up()

    def action_toggle_run(self) -> None:
        if self.bot.is_running():
            self.tui._notice("در حال توقف…")
            self.tui.stop_monitor()
        else:
            if self.bot.monitor is None:
                self.tui._notice("ابتدا از صفحهٔ ورود، وارد شوید")
                return
            self.tui._notice("در حال شروع…")
            self.tui.start_monitor()

    def action_toggle_dry_run(self) -> None:
        new = not self.bot.cfg.dry_run
        self.bot.set_dry_run(new)
        self.tui._notice(f"dry-run {'روشن' if new else 'خاموش'} شد")
        self.refresh_status()

    def action_login(self) -> None:
        if self.bot.monitor is None:
            self.tui._notice("ابتدا از صفحهٔ ورود، وارد شوید")
            return
        threading.Thread(target=self.bot.open_login_browser, daemon=True).start()
        self.tui._notice("پنجرهٔ ورود باز می‌شود…")


class ProjectsScreen(BaseScreen):
    """Every scanned project, with the decision detail behind Enter."""

    SCREEN_NAME = "projects"
    NAV = "پروژه‌ها"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("enter", "open_detail", "جزئیات"),
        Binding("b", "bid", "ارسال پیشنهاد"),
    ]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="جست‌وجو…", id="search")
        yield DataTable(id="projects")
        yield VerticalScroll(Static("", id="detail", classes="card"))

    def on_mount(self) -> None:
        t = self.query_one("#projects", DataTable)
        t.add_columns("قیمت", "عنوان", "همپوشانی", "دلیل")
        t.cursor_type = "row"
        self._rows: list = []
        self.refresh_status()
        # Focus the table, not the search box: with the box focused every
        # digit went into it, so the number-key shortcuts never fired. `/`
        # moves focus into the box when you actually want to type.
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
                      str(p.get("overlap", "") or "—"),
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
            self.tui._notice("پروژه‌ای انتخاب نشده")
            return
        detail = (
            f"[b {bridge.ACCENT}]{p.get('title', '')}[/]\n"
            f"قیمت: {bridge.fmt_money(p.get('price'))}   "
            f"بودجه: {bridge.fmt_money(p.get('budget_min'))}"
            f"–{bridge.fmt_money(p.get('budget_max'))}\n"
            f"رقبا: {p.get('bid_count', '?')}   "
            f"مدت: {p.get('suggested_days', '?')} روز\n"
            f"دلیل: {p.get('reason', '—')}\n"
            f"[{bridge.MUTED}]{str(p.get('url', ''))}[/]")
        self.query_one("#detail", Static).update(detail)

    def action_bid(self) -> None:
        """b: place a manual bid on the highlighted project."""
        if self.bot.cfg.dry_run:
            self.tui._notice("dry-run روشن است — ارسال پیشنهاد غیرفعال")
            return
        p = self._selected()
        if p is None:
            self.tui._notice("پروژه‌ای انتخاب نشده")
            return
        from ponishabot.models import Project
        obj = Project(**{k: p.get(k) for k in Project.__dataclass_fields__})
        self.tui._notice(f"ارسال پیشنهاد برای {str(p.get('title'))[:30]}…")
        threading.Thread(target=self.bot.manual_bid, args=(obj,),
                         daemon=True, name="tui-bid").start()


class SettingsScreen(BaseScreen):
    """Every setting the dashboard exposes, edit-and-save in place.

    Values load from `app.cfg` on entry and write back through the same
    `BotApp.set_*` methods the dashboard uses, so `config.yaml` stays the single
    source of truth and a setting survives a restart.
    """

    SCREEN_NAME = "settings"
    NAV = "تنظیمات"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("ctrl+s", "save", "ذخیره"),
        Binding("enter", "focus_next", "بعدی"),
        Binding("escape", "revert", "لغو"),
        Binding("x", "toggle_switch", "تغییر", show=False),
    ]

    # (config attribute, label, kind) — mirrors the dashboard's sections.
    FIELDS = [
        ("dry_run", "فقط اعلان (dry_run)", "switch"),
        ("headless", "کروم بدون پنجره", "switch"),
        ("show_browser", "نمایش مرورگر زنده", "switch"),
        ("refresh_interval", "بازهٔ بررسی (ثانیه)", "int"),
        ("max_pages", "حداکثر صفحات", "int"),
        ("priority_window_minutes", "پنجرهٔ اولویت (دقیقه)", "int"),
        ("min_skill_match", "حداقل تطابق مهارت", "float"),
        ("min_remaining_proposals", "توقف در سهمیه", "int"),
        ("budget_min", "حداقل بودجه (تومان)", "int"),
        ("budget_max", "حداکثر بودجه (تومان)", "int"),
        ("telegram_token", "توکن تلگرام", "secret"),
        ("telegram_chat_id", "شناسهٔ چت", "text"),
        ("skills", "مهارت‌ها (با کاما)", "list"),
        ("profile_name", "نام", "text"),
        ("profile_about", "دستورالعمل‌های AI", "text"),
        ("profile_hourly_min", "نرخ ساعتی حداقل", "int"),
        ("profile_hourly_max", "نرخ ساعتی حداکثر", "int"),
        ("ai_enabled", "موتور AI", "switch"),
        ("ai_base_url", "آدرس endpoint", "text"),
        ("ai_api_key", "کلید API", "secret"),
        ("ai_model", "مدل", "text"),
        ("ai_min_skill_gate", "آستانهٔ مهارت", "float"),
        ("ai_temperature", "دما", "float"),
        ("ai_max_description_chars", "حداکثر کاراکتر توضیحات", "int"),
        ("karlancer_enabled", "دادهٔ بازار Karlancer", "switch"),
        ("karlancer_max_pages", "صفحات Karlancer", "int"),
        ("high_competition_threshold", "آستانهٔ رقابت بالا", "int"),
        ("urgency_premium_percent", "حباب فوری (%)", "int"),
        ("premium_badge_multiplier", "ضریب بج متمایز", "float"),
        ("low_competition_discount_percent", "رقابت کم (%)", "int"),
        ("high_competition_discount_percent", "رقابت زیاد (%)", "int"),
        ("forex_enabled", "فارکس", "switch"),
        ("baseline_usd_rate", "نرخ مبنا دلار", "int"),
        ("forex_sensitivity", "حساسیت فارکس", "float"),
        ("forex_source", "منبع نرخ", "text"),
        ("karlancer_soft_clamp_enabled", "کلَمپ نرم", "switch"),
        ("karlancer_clamp_multiplier", "ضریب کلَمپ", "float"),
        ("karlancer_target_multiplier", "ضریب هدف", "float"),
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
        self._notice("با Tab بین فیلدها، Ctrl+S ذخیره، Esc لغو")

    def _notice(self, message: str) -> None:
        self.query_one("#notice", Static).update(f"[{bridge.MUTED}]{message}[/]")

    def action_toggle_switch(self) -> None:
        """x flips the switch the cursor is on."""
        try:
            sw = self.screen.focused
            if isinstance(sw, Switch):
                sw.value = not sw.value
        except Exception:
            pass

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
        self._notice("تغییرات ذخیره‌نشده لغو شد")

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
            self._notice(f"ذخیره نشد: {e}")
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
        self._notice("ذخیره شد ✓")


class LoginScreen(BaseScreen):
    """Sign in without leaving the terminal: OTP, or hand off to Chrome."""

    SCREEN_NAME = "login"
    NAV = "ورود"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("enter", "submit", "تأیید"),
        Binding("o", "browser_login", "ورود با مرورگر"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="session", classes="card")
        yield Horizontal(Label("شماره موبایل  ", classes="muted"),
                         Input(placeholder="09121234567", id="mobile"))
        yield Horizontal(Label("کد پیامک      ", classes="muted"),
                         Input(placeholder="------", id="otp"), id="otp-row")
        yield Static("", id="note", classes="card")
        yield Horizontal(Button("درخواست کد", id="btn-request", variant="primary"),
                         Button("ورود با مرورگر", id="btn-browser"))

    def on_mount(self) -> None:
        self.query_one("#otp-row").display = False
        self._mobile = ""
        self.refresh_status()

    def refresh_status(self) -> None:
        from ponishabot.auth import PonishaAuth
        snap = self.tui.snapshot()
        ok = snap.get("has_cookies")
        self.query_one("#session", Static).update(
            f"جلسه: [b {bridge.OK if ok else bridge.DANGER}]"
            f"{'معتبر' if ok else 'نامعتبر — وارد شوید'}[/]"
            f"\n[{bridge.MUTED}]مسیر OTP بدون مرورگر کار می‌کند؛ "
            f"«ورود با مرورگر» کروم را باز می‌کند.[/]")

    def _note(self, message: str, kind: str = "muted") -> None:
        colour = {"ok": bridge.OK, "err": bridge.DANGER}.get(kind, bridge.MUTED)
        self.query_one("#note", Static).update(f"[{colour}]{message}[/]")

    def action_submit(self) -> None:
        """Enter: send the code, or verify it if the code field is showing."""
        from ponishabot.auth import PonishaAuth
        auth = self.bot.auth
        mobile = self.query_one("#mobile", Input).value
        otp = self.query_one("#otp", Input).value

        if mobile and not self._mobile:
            if not PonishaAuth.normalize_mobile(mobile):
                self._note("شمارهٔ موبایل نامعتبر است (مثال: 09121234567)", "err")
                return
            self._mobile = mobile
            self._note("در حال ارسال کد…")
            threading.Thread(target=self._request, args=(mobile,), daemon=True).start()
        elif self._mobile and otp:
            self._note("در حال بررسی کد…")
            threading.Thread(target=self._verify, args=(self._mobile, otp),
                             daemon=True).start()
        else:
            self._note("شماره را وارد کنید، بعد کد پیامک", "err")

    def _request(self, mobile: str) -> None:
        result = self.bot.auth.request_otp(mobile)
        if result.get("ok"):
            self.call_from_thread(self._show_otp_row)
        else:
            self.call_from_thread(self._note,
                                  result.get("error", "ارسال کد ناموفق"), "err")

    def _show_otp_row(self) -> None:
        self.query_one("#otp-row").display = True
        self.query_one("#otp", Input).focus()
        self._note("کد پیامک را وارد کنید و Enter بزنید")

    def _verify(self, mobile: str, otp: str) -> None:
        result = self.bot.auth.verify_otp(mobile, otp)
        if result.get("ok"):
            self._mobile = ""
            self.call_from_thread(self._note, "ورود موفق ✓", "ok")
            self.call_from_thread(self.query_one("#otp-row").__setattr__,
                                  "display", False)
            self.call_from_thread(self.refresh_status)
        else:
            self.call_from_thread(self._note,
                                  result.get("error", "کد نامعتبر"), "err")

    def action_browser_login(self) -> None:
        """o / button: the Chrome path, for when OTP is not set up."""
        if self.bot.monitor is None:
            self._note("ابتدا ربات را از داشبورد شروع کنید", "err")
            return
        threading.Thread(target=self.bot.open_login_browser, daemon=True).start()
        self._note("پنجرهٔ کروم باز می‌شود — وارد شوید، سپس پنجره را ببندید")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-request":
            self.action_submit()
        elif event.button.id == "btn-browser":
            self.action_browser_login()


class LogScreen(BaseScreen):
    """The log, filtered so a healthy run is readable."""

    SCREEN_NAME = "log"
    NAV = "لاگ"

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("f", "cycle_level", "سطح"),
        Binding("n", "toggle_noise", "نویز"),
        Binding("c", "clear", "پاک‌کردن"),
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
        self.query_one("#log-head", Static).update(
            f"سطح [{bridge.ACCENT}]{self.tui.log_level}[/]"
            f"   نویز [{'روشن' if self.tui.show_noise else 'پنهان'}]"
            f"   ردیف {len(collapsed)}"
            f"   [{bridge.MUTED}](f سطح · n نویز · c پاک‌کردن)[/]")

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


ALL_SCREENS = [DashboardScreen, ProjectsScreen, SettingsScreen, LoginScreen, LogScreen]
