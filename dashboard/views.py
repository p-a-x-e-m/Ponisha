"""HTML pages + JSON API for the local ponishabot web GUI.

Blocking BotApp operations always run in daemon threads; views only flip
busy flags and return 202/JSON. Mirror the CustomTkinter _on_* handlers.
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from ponishabot import botstate
from ponishabot.config import ConfigLoader

logger = logging.getLogger(__name__)


def _json(body: dict, status: int = 200) -> JsonResponse:
    return JsonResponse(body, status=status, safe=False)


def _bad(msg: str, status: int = 400) -> JsonResponse:
    return _json({"ok": False, "error": msg}, status=status)


def _busy_flag(key: str) -> bool:
    if botstate.busy.get(key):
        return True
    return False


# ── Pages ──────────────────────────────────────────────────────────
def index(request):
    app = botstate.get_app()
    c = app.cfg
    skills = list(c.search_skills or [])
    summary = {
        "dry_run": bool(c.dry_run),
        "skill_count": len(skills),
        "skills_preview": skills[:6],
        "skills_more": max(0, len(skills) - 6),
        "budget_min": int(c.budget_min or 0),
        "budget_max": int(c.budget_max or 0),
        "refresh_interval": int(c.refresh_interval or 0),
        "ai_enabled": bool(c.ai_enabled),
        # Stored, not verified. ponisha can revoke a cookie at any time, so the
        # template must not read this as "signed in" — `session_valid` carries
        # the verified answer (None until the background probe reports).
        "has_cookies": bool(app.auth.get_cookies()) or bool(app.auth.get_access_token()),
        "session_valid": botstate.session_validity(),
    }
    return render(request, "dashboard/index.html", {"active": "home", "summary": summary})


def settings_page(request):
    app = botstate.get_app()
    c = app.cfg
    cfg = {
        "headless": c.headless,
        "show_browser": c.show_browser,
        "dry_run": c.dry_run,
        "proposal_template": c.proposal_template,
        "user_data_dir": c.user_data_dir or "",
        "telegram_token": c.telegram_token,
        "telegram_chat_id": c.telegram_chat_id,
        "skills": ", ".join(c.search_skills),
        "budget_min": c.budget_min,
        "budget_max": c.budget_max,
        "refresh_interval": c.refresh_interval,
        "priority_window_minutes": c.priority_window_minutes,
        "min_skill_match": c.min_skill_match,
        "max_pages": c.max_pages,
        "min_remaining_proposals": c.min_remaining_proposals,
        "name": c.profile_name,
        "about": c.profile_about,
        "hourly_min": c.profile_hourly_min,
        "hourly_max": c.profile_hourly_max,
        "ai_enabled": c.ai_enabled,
        "ai_base_url": c.ai_base_url,
        "ai_api_key": c.ai_api_key,
        "ai_model": c.ai_model,
        "ai_min_skill_gate": c.ai_min_skill_gate,
        "ai_temperature": c.ai_temperature,
        "ai_max_description_chars": c.ai_max_description_chars,
        "karlancer_enabled": c.karlancer_enabled,
        "karlancer_max_pages": c.karlancer_max_pages,
        "high_competition_threshold": c.high_competition_threshold,
        "urgency_premium_percent": c.urgency_premium_percent,
        "premium_badge_multiplier": c.premium_badge_multiplier,
        "low_competition_discount_percent": c.low_competition_discount_percent,
        "high_competition_discount_percent": c.high_competition_discount_percent,
        "forex_enabled": c.forex_enabled,
        "baseline_usd_rate": c.baseline_usd_rate,
        "forex_sensitivity": c.forex_sensitivity,
        "forex_source": c.forex_source,
        "kl_clamp_enabled": c.karlancer_soft_clamp_enabled,
        "kl_clamp_mult": c.karlancer_clamp_multiplier,
        "kl_target_mult": c.karlancer_target_multiplier,
    }
    return render(request, "dashboard/settings.html", {"active": "settings", "cfg": cfg})


# ── Read APIs ──────────────────────────────────────────────────────
@require_GET
def api_status(request):
    return _json(botstate.status_snapshot())


@require_GET
def api_events(request):
    try:
        since = int(request.GET.get("since", 0))
    except ValueError:
        since = 0
    return _json({"events": botstate.get_events(since), "seq": botstate.latest_event_id()})


@require_GET
def api_logs(request):
    try:
        since = int(request.GET.get("since", 0))
    except ValueError:
        since = 0
    return _json(botstate.read_log_tail(since))


@require_GET
def api_skills(request):
    from ponishabot.paths import SKILLS_CACHE_PATH
    from ponishabot.skills import SkillFetcher

    fetcher = SkillFetcher(SKILLS_CACHE_PATH)
    # load_skills can hit the network — keep request snappy with cache path only
    data = fetcher.load_skills()
    return _json({"categories": data})


# ── Control APIs ───────────────────────────────────────────────────
@require_POST
def api_start(request):
    app = botstate.get_app()
    if app.is_running():
        return _json({"ok": True, "message": "already running"})
    if _busy_flag("starting"):
        return _json({"ok": True, "message": "starting already in progress"})
    botstate.busy["starting"] = True

    def worker():
        try:
            ok = app.start_gui()
            if ok:
                botstate.mark_started()
                botstate.refresh_quota_async()
        except Exception as e:
            logger.error(f"start failed: {e}")
        finally:
            botstate.busy["starting"] = False

    threading.Thread(target=worker, daemon=True, name="web-start").start()
    return _json({"ok": True, "message": "starting"}, status=202)


@require_POST
def api_stop(request):
    app = botstate.get_app()
    if _busy_flag("stopping"):
        return _json({"ok": True, "message": "stopping already in progress"})
    botstate.busy["stopping"] = True

    def worker():
        try:
            app.stop()
        except Exception as e:
            logger.error(f"stop failed: {e}")
        finally:
            botstate.mark_stopped()
            botstate.busy["stopping"] = False

    threading.Thread(target=worker, daemon=True, name="web-stop").start()
    return _json({"ok": True, "message": "stopping"}, status=202)


@require_POST
def api_login(request):
    app = botstate.get_app()
    if _busy_flag("login"):
        return _json({"ok": True, "message": "login already in progress"})
    botstate.busy["login"] = True
    try:
        app.open_login_browser()  # spawns its own worker; events login_done/error
    except Exception as e:
        botstate.busy["login"] = False
        return _bad(str(e), 500)
    # clear flag when login_done/error arrives — also clear on status poll timeout
    def clear_later():
        import time
        time.sleep(600)
        botstate.busy["login"] = False

    # Flag is also cleared in api_login_done; this is a safety net after 10 min
    threading.Thread(target=clear_later, daemon=True).start()
    return _json({"ok": True, "message": "login browser requested"}, status=202)


@require_POST
def api_request_otp(request):
    """Step 1 of OTP login: ask ponisha to SMS a code to the given mobile."""
    app = botstate.get_app()
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _bad("invalid json")
    mobile = str(data.get("mobile") or "").strip()
    if not mobile:
        return _bad("شماره موبایل را وارد کنید")
    if _busy_flag("login"):
        return _bad("یک درخواست ورود در جریان است")
    botstate.busy["login"] = True

    def worker():
        try:
            result = app.auth.request_otp(mobile)
            botstate._otp_result = result  # type: ignore
            if result.get("ok"):
                botstate._otp_mobile = mobile  # type: ignore
            else:
                botstate.busy["login"] = False
        except Exception as e:
            logger.error(f"request_otp failed: {e}")
            botstate._otp_result = {"ok": False, "error": str(e)}  # type: ignore
            botstate.busy["login"] = False

    threading.Thread(target=worker, daemon=True, name="web-otp-request").start()
    return _json({"ok": True, "message": "sending"}, status=202)


@require_POST
def api_verify_otp(request):
    """Step 2 of OTP login: verify the SMS code and store the session token."""
    app = botstate.get_app()
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _bad("invalid json")
    mobile = str(data.get("mobile") or "").strip()
    otp = str(data.get("otp") or "").strip()
    if not mobile or not otp:
        return _bad("موبایل و کد تایید لازم است")
    if _busy_flag("verify"):
        return _bad("یک درخواست تایید در جریان است")
    botstate.busy["verify"] = True

    def worker():
        try:
            result = app.auth.verify_otp(mobile, otp)
            botstate._otp_verify_result = result  # type: ignore
            if result.get("ok"):
                logger.info("OTP login complete — session token stored")
                if not app.auth.get_user_agent():
                    from ponishabot.auth import USER_AGENT
                    app.auth.save_user_agent(USER_AGENT)
                app.event_queue and app.event_queue.put({"type": "login_done"})
            else:
                app.event_queue and app.event_queue.put(
                    {"type": "login_error", "error": result.get("error", "")})
        except Exception as e:
            logger.error(f"verify_otp failed: {e}")
            botstate._otp_verify_result = {"ok": False, "error": str(e)}  # type: ignore
        finally:
            botstate.busy["verify"] = False
            botstate.busy["login"] = False

    threading.Thread(target=worker, daemon=True, name="web-otp-verify").start()
    return _json({"ok": True, "message": "verifying"}, status=202)


@require_GET
def api_otp_status(request):
    """Poll the OTP request/verify results (both run in worker threads)."""
    request_result = getattr(botstate, "_otp_result", None)
    verify_result = getattr(botstate, "_otp_verify_result", None)
    return _json({
        "busy": {"login": bool(botstate.busy.get("login")),
                 "verify": bool(botstate.busy.get("verify"))},
        "request": request_result,
        "verify": verify_result,
    })


@require_POST
def api_login_done(request):
    """User clicked 'I'm logged in' — UI-only banner clear (same as desktop)."""
    botstate.busy["login"] = False
    return _json({"ok": True})


@require_POST
def api_dry_run(request):
    try:
        data = json.loads(request.body or b"{}")
        dry = bool(data.get("dry_run"))
    except json.JSONDecodeError:
        return _bad("invalid json")
    app = botstate.get_app()
    app.set_dry_run(dry)
    return _json({"ok": True, "dry_run": app.cfg.dry_run})


@require_POST
def api_bid(request):
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _bad("invalid json")
    pid = str(data.get("project_id") or "")
    project = botstate.get_project(pid)
    if not project:
        return _bad("unknown project", 404)
    app = botstate.get_app()
    if app.cfg.dry_run:
        return _bad("DRY_RUN is on — bidding disabled")
    if _busy_flag("bid"):
        return _json({"ok": True, "message": "bid already in progress"}, status=202)

    from ponishabot.models import Project

    obj = Project(**{k: project.get(k) for k in Project.__dataclass_fields__})
    botstate.busy["bid"] = True

    def worker():
        try:
            ok = app.manual_bid(obj)
            logger.info(f"manual_bid({pid}) -> {ok}")
        except Exception as e:
            logger.error(f"manual_bid failed: {e}")
        finally:
            botstate.busy["bid"] = False

    threading.Thread(target=worker, daemon=True, name="web-bid").start()
    return _json({"ok": True, "message": "bid started"}, status=202)


@require_POST
def api_settings(request):
    try:
        form = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _bad("invalid json")

    app = botstate.get_app()
    try:
        skills = form.get("skills")
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        if not isinstance(skills, list):
            skills = list(app.cfg.search_skills)

        bmin = int(form.get("budget_min", app.cfg.budget_min) or 0)
        bmax = int(form.get("budget_max", app.cfg.budget_max) or 0)
        interval = int(form.get("refresh_interval", app.cfg.refresh_interval) or 300)
        hmin = int(form.get("hourly_min", app.cfg.profile_hourly_min) or 0)
        hmax = int(form.get("hourly_max", app.cfg.profile_hourly_max) or 0)
        min_skill = float(form.get("min_skill_match", app.cfg.min_skill_match) or 0.5)
        min_proposals = int(form.get("min_remaining_proposals", app.cfg.min_remaining_proposals) or 2)
        max_pages = int(form.get("max_pages", app.cfg.max_pages) or 3)
        high_comp = int(form.get("high_competition_threshold", app.cfg.high_competition_threshold) or 15)
        urgency_pct = int(form.get("urgency_premium_percent", app.cfg.urgency_premium_percent) or 20)
        premium_mult = float(form.get("premium_badge_multiplier", app.cfg.premium_badge_multiplier) or 1.15)
        low_comp = int(form.get("low_competition_discount_percent", app.cfg.low_competition_discount_percent) or 10)
        high_disc = int(form.get("high_competition_discount_percent", app.cfg.high_competition_discount_percent) or 15)
        ai_gate = float(form.get("ai_min_skill_gate", app.cfg.ai_min_skill_gate) or 0.2)
        ai_temp = float(form.get("ai_temperature", app.cfg.ai_temperature) or 0.2)
        ai_desc = int(form.get("ai_max_description_chars", app.cfg.ai_max_description_chars) or 4000)
        kpages = int(form.get("karlancer_max_pages", app.cfg.karlancer_max_pages) or 2)
        prio = int(form.get("priority_window_minutes", app.cfg.priority_window_minutes) or 5)
        baseline = int(form.get("baseline_usd_rate", app.cfg.baseline_usd_rate) or 229000)
        sens = float(form.get("forex_sensitivity", app.cfg.forex_sensitivity) or 0.5)
        clamp = float(form.get("kl_clamp_mult", app.cfg.karlancer_clamp_multiplier) or 3.0)
        target = float(form.get("kl_target_mult", app.cfg.karlancer_target_multiplier) or 2.5)
    except (TypeError, ValueError, OverflowError) as e:
        # OverflowError: json.loads accepts Infinity/NaN literals, and
        # int(float('inf')) raises. A NaN reaching config.yaml poisons every
        # later comparison, so reject non-finite floats outright.
        return _bad(f"invalid number: {e}")
    for _name, _val in (("min_skill_match", min_skill), ("premium_badge_multiplier", premium_mult),
                        ("ai_min_skill_gate", ai_gate), ("ai_temperature", ai_temp),
                        ("forex_sensitivity", sens), ("kl_clamp_mult", clamp),
                        ("kl_target_mult", target)):
        if _val != _val or _val in (float("inf"), float("-inf")):
            return _bad(f"invalid number: {_name}")

    interval = max(10, min(3600, interval))
    about = str(form.get("about", app.cfg.profile_about) or "").strip()
    name = str(form.get("name", app.cfg.profile_name) or "")

    app.set_telegram(
        str(form.get("telegram_token", app.cfg.telegram_token) or ""),
        str(form.get("telegram_chat_id", app.cfg.telegram_chat_id) or ""),
    )
    app.set_profile(
        skills=skills, min_budget=bmin, max_budget=bmax,
        name=name, about=about, hourly_min=hmin, hourly_max=hmax,
    )
    app.cfg.ai_min_skill_gate = ai_gate
    app.cfg.ai_temperature = ai_temp
    app.cfg.ai_max_description_chars = ai_desc
    app.cfg.karlancer_enabled = bool(form.get("karlancer_enabled", app.cfg.karlancer_enabled))
    app.cfg.karlancer_max_pages = kpages
    app.set_ai_config(
        enabled=bool(form.get("ai_enabled", app.cfg.ai_enabled)),
        api_key=str(form.get("ai_api_key", app.cfg.ai_api_key) or ""),
        model=str(form.get("ai_model", app.cfg.ai_model) or ""),
        base_url=str(form.get("ai_base_url", app.cfg.ai_base_url) or ""),
    )
    app.cfg.min_skill_match = min_skill
    app.cfg.min_remaining_proposals = min_proposals
    app.cfg.max_pages = max_pages
    app.cfg.high_competition_threshold = high_comp
    app.cfg.urgency_premium_percent = urgency_pct
    app.cfg.premium_badge_multiplier = premium_mult
    app.cfg.low_competition_discount_percent = low_comp
    app.cfg.high_competition_discount_percent = high_disc
    app.set_refresh_interval(interval)
    app.cfg.priority_window_minutes = prio
    app.cfg.proposal_template = str(form.get("proposal_template", app.cfg.proposal_template) or "").strip()
    app.cfg.user_data_dir = str(form.get("user_data_dir") or "").strip() or None
    app.cfg.forex_enabled = bool(form.get("forex_enabled", app.cfg.forex_enabled))
    app.cfg.baseline_usd_rate = baseline
    app.cfg.forex_sensitivity = sens
    app.cfg.forex_source = str(form.get("forex_source", app.cfg.forex_source) or "wallex").strip()
    app.cfg.karlancer_soft_clamp_enabled = bool(form.get("kl_clamp_enabled", app.cfg.karlancer_soft_clamp_enabled))
    app.cfg.karlancer_clamp_multiplier = clamp
    app.cfg.karlancer_target_multiplier = target
    app.cfg.headless = bool(form.get("headless", app.cfg.headless))
    app.cfg.show_browser = bool(form.get("show_browser", app.cfg.show_browser))
    if "dry_run" in form:
        app.set_dry_run(bool(form.get("dry_run")))
    ConfigLoader.save(app.cfg)
    return _json({"ok": True, "message": f"Saved — poll {interval}s"})


@require_POST
def api_test_ai(request):
    app = botstate.get_app()
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        data = {}
    if _busy_flag("test_ai"):
        return _json({"ok": True, "busy": True})
    botstate.busy["test_ai"] = True
    key = str(data.get("api_key") or "")
    model = str(data.get("model") or "")

    def worker():
        try:
            ok, detail = app.test_ai(key, model)
            logger.info(f"Test AI: {ok} — {detail}")
            botstate._test_ai_result = {"ok": ok, "detail": detail}  # type: ignore
        except Exception as e:
            logger.error(f"Test AI failed: {e}")
            botstate._test_ai_result = {"ok": False, "detail": str(e)}  # type: ignore
        finally:
            botstate.busy["test_ai"] = False

    threading.Thread(target=worker, daemon=True).start()
    return _json({"ok": True, "message": "testing…"}, status=202)


@require_POST
def api_test_tg(request):
    app = botstate.get_app()
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        data = {}
    token = str(data.get("token") or app.cfg.telegram_token or "").strip()
    chat_id = str(data.get("chat_id") or app.cfg.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return _bad("enter Bot Token and Chat ID first")
    if _busy_flag("test_tg"):
        return _json({"ok": True, "busy": True})
    botstate.busy["test_tg"] = True

    def worker():
        try:
            from ponishabot.notifier import TelegramNotifier
            ok = TelegramNotifier(token, chat_id).send(
                "✅ ponishabot: Telegram connection test successful!"
            )
            logger.info(f"Telegram test: {'OK' if ok else 'FAILED'}")
        except Exception as e:
            logger.error(f"Telegram test error: {e}")
        finally:
            botstate.busy["test_tg"] = False

    threading.Thread(target=worker, daemon=True).start()
    return _json({"ok": True, "message": "sending…"}, status=202)


@require_POST
def api_demo(request):
    if _busy_flag("demo"):
        return _json({"ok": True, "busy": True})
    botstate.busy["demo"] = True

    def worker():
        try:
            from ponishabot.decision import HeuristicBidDecision
            from ponishabot.models import Profile, Project

            logger.info("Running demo with mock data...")
            prof = Profile(
                skills=["Django", "Python", "PostgreSQL", "SQL", "Docker"],
                min_budget=2_000_000, max_budget=25_000_000,
                name="Demo", about="Demo profile", hourly_min=80_000, hourly_max=150_000,
            )
            dec = HeuristicBidDecision(min_skill_match=0.2)
            mocks = [
                Project(id="demo1", title="Demo CRUD Django API",
                        url="https://example.com/1",
                        budget_min=5_000_000, budget_max=8_000_000,
                        skills=["Django", "Python"], bid_count=3),
                Project(id="demo2", title="Demo frontend only",
                        url="https://example.com/2",
                        budget_min=1_000_000, budget_max=2_000_000,
                        skills=["React"], bid_count=0),
            ]
            for p in mocks:
                plan = dec.decide(p, prof)
                logger.info(f"Demo {p.id}: should_bid={plan.should_bid} price={plan.price} — {plan.message}")
        except Exception as e:
            logger.error(f"Demo failed: {e}")
        finally:
            botstate.busy["demo"] = False

    threading.Thread(target=worker, daemon=True).start()
    return _json({"ok": True, "message": "demo running"}, status=202)


@require_POST
def api_open_project(request):
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _bad("invalid json")
    url = str(data.get("url") or "").strip()
    # This opens a browser ON THE BOT HOST, so the target must be pinned to
    # ponisha. A bare startswith("http") would accept any scheme-relative or
    # attacker-chosen URL and launch it on the operator's desktop.
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
            host == "ponisha.ir" or host.endswith(".ponisha.ir")):
        return _bad("invalid url")
    try:
        webbrowser.open(url)
    except Exception as e:
        # Log the detail, return a generic message: an exception from the
        # desktop opener can carry local paths and environment specifics, and
        # this response crosses the network boundary.
        logger.warning(f"open_project failed: {type(e).__name__}: {e}")
        return _bad("could not open the URL on the host", 500)
    return _json({"ok": True})
