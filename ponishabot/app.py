"""Central BotApp controller — wires all components together.

The monitor runs in a daemon thread; events are passed to the GUI (or to the
terminal log) via event_queue. Communication is fully thread-safe: no tkinter
calls are made from the monitor thread.

Threading model for the shared Selenium driver (auth.driver):
  - Only one driver may run at a time. Every driver-using operation (live
    browser window, manual login, bid submission) holds self._lock for the
    duration of its driver access, so a bid never races the live refresher.
"""

import json
import logging
import os
import queue
import signal
import sys
import threading

from ponishabot import paths
from ponishabot.config import ConfigLoader
from ponishabot.models import Profile
from ponishabot.auth import PonishaAuth
from ponishabot.scraper import PonishaScraper
from ponishabot.decision import HeuristicBidDecision, LLMBidDecision
from ponishabot.notifier import TelegramNotifier
from ponishabot.monitor import ProjectMonitor
from ponishabot.bidder import PonishaBidder

logger = logging.getLogger(__name__)



class BotApp:
    def __init__(self, cfg):
        self.cfg = cfg

        self.profile = Profile(
            skills=cfg.search_skills,
            min_budget=cfg.budget_min,
            max_budget=cfg.budget_max,
            name=cfg.profile_name,
            about=cfg.profile_about,
            hourly_min=cfg.profile_hourly_min,
            hourly_max=cfg.profile_hourly_max,
        )
        self.notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
        self.auth = PonishaAuth(headless=cfg.headless, user_data_dir=cfg.user_data_dir)

        self.scraper: "PonishaScraper | None" = None
        self.monitor: "ProjectMonitor | None" = None
        self.bidder: "PonishaBidder | None" = None
        self.decision: "object | None" = None
        self._thread: "threading.Thread | None" = None
        self._lock = threading.RLock()         # guards Selenium driver access
        self._cfg_lock = threading.RLock()     # guards cfg mutation + YAML writes
        self._bidding = False                  # a bid is mid-flight (holds _lock)
        self._bid_log_unreadable = False       # set by _load_bid_log; blocks saves
        self._stop_live = threading.Event()
        self.event_queue: "queue.Queue | None" = None
        self._proposal = ""

        self._rebuild_decision()

    # ─── Auth ───
    def ensure_auth(self, interactive: bool = True) -> bool:
        """If no cookie is stored, open Chrome for manual login."""
        return self.auth.auth(interactive=interactive)

    def has_stored_session(self) -> bool:
        """Whether anything usable is stored to authenticate with.

        `has_valid_session()` is the honest answer but it probes ponisha first,
        so it can block for the length of a request timeout. A button handler
        runs on the UI thread and cannot afford that, so this answers the
        cheaper question instead: is there a cookie or an OTP token to try at
        all. `start()` then does the real verification on its worker thread.
        """
        return bool(self.auth.get_cookies() or self.auth.get_access_token())

    def has_valid_session(self) -> bool:
        """Whether a usable session exists, without opening a browser.

        `status_snapshot()["has_cookies"]` is not enough: this machine has a
        stored cookie ponisha has since invalidated, so that check reports True
        and the caller then builds the monitor — which opens Chrome and blocks
        on `input()` in a plain run. Probing the API is what actually answers
        the question; the auth layer already does exactly this before deciding
        to ask for a manual login.
        """
        cookies = self.auth.get_cookies() or []
        if cookies and self.auth._probe_session(cookies):
            return True
        # An OTP login stores a bearer token rather than cookies; the auth layer
        # can still synthesise a working session from it without a browser.
        if self.auth._token_cookies():
            return True
        # Synthesising that cookie needs the pss-at passphrase. Without one the
        # call above returns nothing, and this used to answer "no session" for a
        # token the API was still accepting — which sent the plain run down the
        # "not authenticated" path and started nothing.
        return self.auth.has_live_token()

    # ─── Status ───
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_bidding(self) -> bool:
        return self._bidding

    # ─── Headless ───
    def set_headless(self, headless: bool) -> None:
        """Toggle headless (invisible) mode — only effective before start.
        The settings page writes cfg.headless directly and documents that it
        takes effect after a restart; this stays for API/CLI callers."""
        with self._cfg_lock:
            self.cfg.headless = bool(headless)
            self.auth.headless = bool(headless)
            ConfigLoader.save(self.cfg)

    # ─── Events ───
    def _on_event(self, event: dict) -> None:
        if self.event_queue is not None:
            try:
                self.event_queue.put(event)
            except Exception:
                pass

    # ─── Bid submission (single implementation for GUI + terminal) ───
    def _load_proposal(self) -> str:
        """Load fallback proposal template. With AI enabled, the AI generates
        a unique proposal per project — this template is only used when
        AI fails or DRY_RUN mode. Returns empty string if file not found."""
        path = paths.root_path(self.cfg.proposal_template)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read().strip()
                if text:
                    logger.debug(f"Loaded fallback proposal template ({len(text)} chars)")
                return text
        except FileNotFoundError:
            logger.debug("No fallback proposal template found — AI will generate proposals")
            return ""
        except Exception as e:
            logger.warning(f"Could not load proposal template: {e}")
            return ""

    def _load_bid_log(self) -> set:
        """Load persisted bid ids (attempt semantics) from bid_log.json.

        Returns an empty set when the file is genuinely absent, but sets
        self._bid_log_unreadable when it exists and cannot be parsed. That flag
        stops _save_bid_log from overwriting a file we failed to read — a
        half-written file used to be treated as "no bids yet", and the next
        save then erased every id, re-arming projects that were already bid on.
        """
        self._bid_log_unreadable = False
        try:
            with open(paths.BID_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return set()
        except Exception as e:
            self._bid_log_unreadable = True
            logger.warning(f"Could not read bid_log ({e}) — keeping the file as-is")
            return set()
        if not isinstance(data, list):
            self._bid_log_unreadable = True
            logger.warning("bid_log is not a list — keeping the file as-is")
            return set()
        ids = {str(x) for x in data}
        if ids:
            logger.info(f"Loaded {len(ids)} persisted bid(s)")
        return ids

    def _save_bid_log(self, ids) -> None:
        """Rewrite bid_log.json atomically (cap ~5000).

        Written to a temp file and renamed into place: truncating in place left
        a window where any reader (including our own status poll) saw a partial
        file, and a partial file parsed as "no bids".
        """
        if getattr(self, "_bid_log_unreadable", False):
            logger.error("Refusing to overwrite an unreadable bid_log — "
                         "fix or delete %s manually", paths.BID_LOG_PATH)
            return
        try:
            # Preserve insertion-ish order: previously saved first, then new
            ordered = list(ids)[-5000:]
            path = paths.BID_LOG_PATH
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ordered, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on the same filesystem
        except Exception as e:
            logger.warning(f"Could not persist bid_log: {e}")

    def _submit_bid_locked(self, project, plan) -> bool:
        """Run the Selenium bid while holding the driver lock, then report.
        Returns True iff the submitter reported success."""
        self._bidding = True
        ok = False
        try:
            with self._lock:
                if not self.cfg.dry_run and self.bidder is None:
                    self._proposal = self._load_proposal()
                    self.bidder = PonishaBidder(self.auth, self._proposal)
                if self.bidder is None:
                    logger.warning("Bidder not available — bid skipped")
                    return False
                verdict = self.bidder.submit(project, plan)
            # bidder.submit reports "submitted" / "unconfirmed" / "aborted".
            # Only the first two mean the form was actually sent: "unconfirmed"
            # may have landed, so we record it to avoid double-bidding.
            # "aborted" means the click never happened, so the project must
            # stay retryable — recording it would burn it on a transient error.
            ok = verdict == "submitted"
            if ok:
                self.notifier.notify_bid(project.title, plan.price, plan.duration_days)
            elif verdict == "unconfirmed":
                logger.warning(f"Bid unconfirmed for {project.id} — recording to "
                               f"avoid a duplicate; verify in 'My proposals'")
                self.notifier.notify_error(
                    f"Bid unconfirmed for: {project.title[:40]}")
            else:
                logger.warning(f"Bid aborted for {project.id} — will retry later")
                self.notifier.notify_error(f"Bid failed for: {project.title[:40]}")

            if verdict in ("submitted", "unconfirmed") and self.monitor is not None:
                self.monitor.bid_log.add(project.id)
                self._save_bid_log(self.monitor.bid_log)

            self._on_event({"type": "bid", "title": project.title,
                            "price": plan.price, "success": ok})
            return ok
        finally:
            self._bidding = False

    # ─── Start ───
    def start(self, interactive: bool = True) -> bool:
        """Build session/scraper/monitor and run in a background thread."""
        if self.is_running():
            logger.warning("Bot is already running")
            return True

        if not self.ensure_auth(interactive=interactive):
            logger.critical("Authentication failed")
            return False

        session = self.auth.build_session()
        self.scraper = PonishaScraper(session,
                                      api_session=self.auth.build_api_session())
        self.monitor = ProjectMonitor(
            scraper=self.scraper,
            decision=self.decision,
            notifier=self.notifier,
            profile=self.profile,
            refresh_interval=self.cfg.refresh_interval,
            max_pages=self.cfg.max_pages,
            dry_run=self.cfg.dry_run,
            min_remaining_proposals=self.cfg.min_remaining_proposals,
            priority_window_minutes=self.cfg.priority_window_minutes,
        )
        self.monitor.add_observer(self._on_event)
        self.monitor.bid_log = self._load_bid_log()
        # Always attach the real submitter; dry_run is the only bid gate
        # (monitor.run_once). Toggling dry_run mid-run then works without rewiring.
        self.monitor._submit_bid = self._submit_bid_locked
        if not self.cfg.dry_run:
            self._proposal = self._load_proposal()
            self.bidder = PonishaBidder(self.auth, self._proposal)

        self._thread = threading.Thread(target=self.monitor.start, daemon=True)
        self._thread.start()
        logger.info("Bot started in background thread")

        # Optional live browser window alongside monitoring (dashboard view)
        if self.cfg.show_browser:
            self._open_live_browser()
        return True

    def start_gui(self) -> bool:
        """Start the bot from the GUI. Uses interactive=False so no blocking
        input() prompt is shown — the browser still opens for manual login."""
        return self.start(interactive=False)

    def open_login_browser(self) -> None:
        """Open the browser to the login page and wait for manual login.

        Emits a 'login_done' / 'login_error' event when finished.

        The monitor is built here if it is not already. It used to be built in
        `run_terminal` before the TUI started, which signed in first and
        blocked on `input()` from the main thread, so the interface never
        appeared. Doing it now keeps that work on this worker thread instead.
        """
        def worker():
            try:
                if self.monitor is None and not self.build_monitor():
                    logger.error("Login browser: could not prepare the bot")
                    self.event_queue and self.event_queue.put(
                        {"type": "login_error", "error": "could not prepare the bot"})
                    return
                with self._lock:
                    self.auth.manual_login(interactive=False)
                    self.auth.save_cookies(
                        self.auth.driver.get_cookies() if self.auth.driver else [])
                self.event_queue and self.event_queue.put({"type": "login_done"})
                logger.info("Login browser session saved")
            except Exception as e:
                logger.error(f"Login browser error: {e}")
                self.event_queue and self.event_queue.put({"type": "login_error", "error": str(e)})
        threading.Thread(target=worker, daemon=True).start()

    def _open_live_browser(self) -> None:
        """Open a visible Chrome window showing ponisha's project listing
        as a live dashboard while the (requests-based) monitor runs.
        The page auto-refreshes on the same cadence as the monitor. The
        refresh loop releases the driver lock between refreshes so bids can
        run; a bid in flight simply makes the next refresh wait."""
        prev_headless = self.auth.headless
        # Clear here, on the caller's thread, so a stop() that arrives before
        # the worker is scheduled is not undone by the worker clearing the flag
        # and opening a browser that nothing will ever close.
        self._stop_live.clear()

        def worker():
            if self._stop_live.is_set():
                return  # stopped before we got scheduled — do not open Chrome
            try:
                with self._lock:
                    if self._stop_live.is_set():
                        return
                    if self.auth.driver is None:
                        self.auth.headless = False  # live view must be visible
                        try:
                            self.auth.inject_cookies_into_driver()
                        finally:
                            self.auth.headless = prev_headless
                        self.auth.driver.get(f"{self.auth.base_url}/search/projects")
                self.event_queue and self.event_queue.put({"type": "live_browser"})
                logger.info("Live browser window opened")
                interval = max(self.cfg.refresh_interval, 20)
                while self.auth.driver is not None and not self._stop_live.is_set():
                    self._stop_live.wait(interval)
                    if self._stop_live.is_set() or self.auth.driver is None:
                        break
                    try:
                        with self._lock:
                            if self.auth.driver is not None and not self._bidding:
                                self.auth.driver.refresh()
                    except Exception:
                        break
            except Exception as e:
                logger.warning(f"Live browser could not open: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def close_live_browser(self) -> None:
        """Stop the refresh loop and close the live browser window.
        Waits for the driver lock so an in-flight bid is never killed."""
        self._stop_live.set()
        with self._lock:
            self.auth.close_driver()

    # ─── Stop ───
    def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self._stop_live.set()
        if self._thread is not None:
            # Short join — don't block the GUI for up to 60s.  The monitor's
            # sleep is interruptible so it usually exits quickly; if a long
            # scan cycle (Karlancer + AI) is in-flight, just log and move on.
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                # Keep the reference: is_running() stays True so start()
                # cannot spawn a second concurrent monitor.
                logger.warning("Monitor thread still finishing its cycle (non-blocking stop)")
            else:
                self._thread = None
        if self._lock.acquire(timeout=10):
            try:
                self.auth.close_driver()
            finally:
                self._lock.release()
        else:
            logger.warning("Driver busy (bid in flight?) — Chrome left open")
        logger.info("Bot stopped")

    # ─── Telegram ───
    def set_telegram(self, token: str, chat_id: str) -> None:
        """Update Telegram credentials and rebuild the live notifier
        (monitor keeps its own reference to the notifier object)."""
        token = (token or "").strip()
        chat_id = (chat_id or "").strip()
        with self._cfg_lock:
            self.cfg.telegram_token = token
            self.cfg.telegram_chat_id = chat_id
            self.notifier = TelegramNotifier(token, chat_id)
            if self.monitor is not None:
                self.monitor.notifier = self.notifier
            ConfigLoader.save(self.cfg)
        logger.info(f"Telegram notifier updated (enabled={self.notifier.enabled})")

    # ─── Live settings update ───
    def set_profile(self, skills=None, min_budget=None, max_budget=None,
                     name=None, about=None, hourly_min=None,
                     hourly_max=None) -> None:
        # monitor.profile IS self.profile (same object) — one update suffices
        with self._cfg_lock:
            if skills is not None:
                self.cfg.search_skills = list(skills)
                self.profile.skills = list(skills)
            if min_budget is not None:
                self.cfg.budget_min = int(min_budget)
                self.profile.min_budget = int(min_budget)
            if max_budget is not None:
                self.cfg.budget_max = int(max_budget)
                self.profile.max_budget = int(max_budget)
            if name is not None:
                self.cfg.profile_name = name.strip()
                self.profile.name = name.strip()
            if about is not None:
                self.cfg.profile_about = about.strip()
                self.profile.about = about.strip()
            if hourly_min is not None:
                self.cfg.profile_hourly_min = int(hourly_min)
                self.profile.hourly_min = int(hourly_min)
            if hourly_max is not None:
                self.cfg.profile_hourly_max = int(hourly_max)
                self.profile.hourly_max = int(hourly_max)
            ConfigLoader.save(self.cfg)

    def set_refresh_interval(self, seconds: int) -> None:
        with self._cfg_lock:
            self.cfg.refresh_interval = int(seconds)
            if self.monitor is not None:
                self.monitor.refresh_interval = int(seconds)
            ConfigLoader.save(self.cfg)

    def set_dry_run(self, dry_run: bool) -> None:
        with self._cfg_lock:
            self.cfg.dry_run = dry_run
            if self.monitor is not None:
                self.monitor.dry_run = dry_run
            ConfigLoader.save(self.cfg)

    # ─── AI decision engine ───
    def _rebuild_decision(self) -> None:
        """Pick the decision engine from config: AI when fully configured,
        otherwise the heuristic. Swaps the live monitor's engine too."""
        with self._cfg_lock:
            if (self.cfg.ai_enabled and self.cfg.ai_api_key
                    and self.cfg.ai_model and self.cfg.ai_base_url):
                from ponishabot.karlancer_scraper import KarlancerScraper
                from ponishabot.forex import ForexProvider
                karlancer = KarlancerScraper(self.auth.build_session()) if self.cfg.karlancer_enabled else None
                forex = ForexProvider(source=self.cfg.forex_source) if self.cfg.forex_enabled else None
                self.decision = LLMBidDecision(
                    api_key=self.cfg.ai_api_key,
                    base_url=self.cfg.ai_base_url,
                    model=self.cfg.ai_model,
                    min_skill_gate=self.cfg.ai_min_skill_gate,
                    temperature=self.cfg.ai_temperature,
                    max_description_chars=self.cfg.ai_max_description_chars,
                    fallback_min_skill_match=self.cfg.min_skill_match,
                    karlancer_scraper=karlancer,
                    karlancer_enabled=self.cfg.karlancer_enabled,
                    karlancer_max_pages=self.cfg.karlancer_max_pages,
                    forex_provider=forex,
                    forex_enabled=self.cfg.forex_enabled,
                    baseline_usd_rate=self.cfg.baseline_usd_rate,
                    forex_sensitivity=self.cfg.forex_sensitivity,
                    karlancer_soft_clamp_enabled=self.cfg.karlancer_soft_clamp_enabled,
                    karlancer_clamp_multiplier=self.cfg.karlancer_clamp_multiplier,
                    karlancer_target_multiplier=self.cfg.karlancer_target_multiplier,
                )
                logger.info("Decision engine: AI (%s) with heuristic fallback",
                            self.cfg.ai_model)
            else:
                self.decision = HeuristicBidDecision(
                    min_skill_match=self.cfg.min_skill_match,
                    high_competition_threshold=self.cfg.high_competition_threshold,
                    urgency_premium_percent=self.cfg.urgency_premium_percent,
                    premium_badge_multiplier=self.cfg.premium_badge_multiplier,
                    low_competition_discount_percent=self.cfg.low_competition_discount_percent,
                    high_competition_discount_percent=self.cfg.high_competition_discount_percent,
                )
                logger.info("Decision engine: heuristic (AI off or not configured)")
            if self.monitor is not None:
                self.monitor.decision = self.decision  # GIL-atomic live swap

    def set_ai_config(self, enabled=None, api_key=None, model=None,
                      base_url=None) -> None:
        with self._cfg_lock:
            if enabled is not None:
                self.cfg.ai_enabled = bool(enabled)
            if api_key is not None:
                self.cfg.ai_api_key = api_key.strip()
            if model is not None:
                self.cfg.ai_model = model.strip()
            if base_url is not None and base_url.strip():
                self.cfg.ai_base_url = base_url.strip()
            self._rebuild_decision()
            ConfigLoader.save(self.cfg)

    def test_ai(self, api_key: str = "", model: str = ""):
        """Probe the endpoint; uses passed credentials or the saved ones.
        Returns (ok, detail). Blocking — call from a worker thread."""
        probe = LLMBidDecision(
            api_key=api_key or self.cfg.ai_api_key,
            base_url=self.cfg.ai_base_url,
            model=model or self.cfg.ai_model,
        )
        return probe.test_connection()

    # ─── Proposal quota ───
    def get_remaining_proposals(self) -> "int | None":
        """Query the REST API for the user's remaining proposal quota.
        None = unknown (quota API unavailable)."""
        if self.scraper is not None:
            return self.scraper.fetch_remaining_proposals()
        api = self.auth.build_api_session()
        if api is None:
            return None
        return PonishaScraper(self.auth.build_session(),
                              api_session=api).fetch_remaining_proposals(api)

    # ─── Manual bid ───
    def manual_bid(self, project) -> bool:
        """Place a manual bid on a project — plan computed from the profile.
        Works without Start: builds the scraper session on demand."""
        if self.scraper is None:
            if not self.ensure_auth(interactive=False):
                logger.error("Manual bid: authentication required")
                return False
            self.scraper = PonishaScraper(
                self.auth.build_session(),
                api_session=self.auth.build_api_session())
        if not self.cfg.dry_run:
            has_api = getattr(self.scraper, "_api_session", None) is not None
            remaining = self.scraper.fetch_remaining_proposals()
            threshold = self.cfg.min_remaining_proposals
            if remaining is None and has_api:
                if self.monitor is not None:
                    self.monitor._quota_fail_strikes += 1
                    if self.monitor._quota_fail_strikes >= 2:
                        logger.warning("Quota API unavailable — refusing manual bid")
                        self.notifier.notify_error("Quota API unavailable — manual bid blocked")
                        return False
            if remaining is not None and remaining <= threshold:
                logger.warning(
                    f"Manual bid refused: only {remaining} proposals left "
                    f"(threshold {threshold})")
                self.notifier.notify_quota_low(remaining)
                return False
            if self.bidder is None:
                self._proposal = self._load_proposal()
                self.bidder = PonishaBidder(self.auth, self._proposal)
        if self.monitor is not None and project.id in self.monitor.bid_log:
            logger.info("This project was already bid on")
            return False
        enriched = self.scraper.fetch_detail(project)
        if enriched is None:
            logger.warning(f"Could not load project detail for {project.id}")
            return False
        plan = self.decision.decide(enriched, self.profile)
        if not plan.should_bid:
            logger.info(f"Decision: no bid — {plan.message}")
            return False
        return bool(self._submit_bid_locked(enriched, plan))

    # ─── Terminal mode ───
    def build_monitor(self) -> bool:
        """Authenticate and wire the monitor, without starting it.

        Shared by the terminal panel and `start()`. The panel needs the monitor
        object before anything runs so it can attach its observer, and it owns
        the main thread, so it cannot call `start()` and then render — the
        monitor has to be launched separately, in a background thread.
        """
        if self.monitor is not None:
            return True
        if not self.ensure_auth(interactive=True):
            logger.critical("Authentication failed")
            return False

        session = self.auth.build_session()
        # api_session gives the scraper access to api.ponisha.ir so the
        # proposal-quota gate works here exactly as it does in the GUI.
        self.scraper = PonishaScraper(session,
                                      api_session=self.auth.build_api_session())
        self.monitor = ProjectMonitor(
            scraper=self.scraper,
            decision=self.decision,
            notifier=self.notifier,
            profile=self.profile,
            refresh_interval=self.cfg.refresh_interval,
            max_pages=self.cfg.max_pages,
            dry_run=self.cfg.dry_run,
            min_remaining_proposals=self.cfg.min_remaining_proposals,
            priority_window_minutes=self.cfg.priority_window_minutes,
        )
        self.monitor.add_observer(self._on_event)
        self.monitor.bid_log = self._load_bid_log()
        self.monitor._submit_bid = self._submit_bid_locked
        if not self.cfg.dry_run:
            self._proposal = self._load_proposal()
            self.bidder = PonishaBidder(self.auth, self._proposal)
        return True

    def start_monitor_thread(self) -> threading.Thread:
        """Run the monitor loop in a daemon thread and return it.

        The terminal panel renders and reads keys on the main thread, so the
        monitor cannot run there; this mirrors what `start()` does for the web
        dashboard.
        """
        self._thread = threading.Thread(target=self.monitor.start, daemon=True)
        self._thread.start()
        logger.info("Monitor started in background thread (terminal panel)")
        return self._thread

    def run_terminal(self, plain: bool = False, level: str = "important") -> None:
        """Terminal entry: hand over to the TUI, signing in only if asked.

        No authentication happens here. `build_monitor()` signs in, and with no
        stored session that means `manual_login()` — which opens Chrome and
        blocks on `input()` from the main thread. Starting `main.py` therefore
        showed a login prompt first and the TUI never appeared at all. The Login
        screen exists for exactly that step, so it is left to the operator.
        """
        from ponishabot.tui import can_run_tui, run_plain, run_tui

        # Decide the mode BEFORE touching authentication: a piped run must not
        # open a browser it has no way to show.
        if plain or not can_run_tui():
            run_plain(self)
            return

        def handle_interrupt(signum, frame):
            # Raising here lets the TUI's own KeyboardInterrupt path run, so the
            # driver is closed by the `finally` below rather than leaving a
            # Chrome window behind.
            logger.info("Interrupt signal received, shutting down...")
            raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGINT, handle_interrupt)
        except (ValueError, AttributeError):
            pass

        try:
            run_tui(self, level=level)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
        finally:
            self._stop_live.set()
            self.stop()
            with self._lock:
                self.auth.close_driver()
            logger.info("Clean exit")
