"""Shared in-process state for the Django web GUI (and optional mgmt commands).

One process owns one BotApp + monitor thread + Selenium driver. Views must
never block on start/stop/login/bid — spawn threads using the busy helpers.

BotApp's event_queue is a plain queue.Queue injected here; a drainer thread
serializes events into a ring buffer that GET /api/events?since= serves.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import queue
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_app: Any = None
_app_lock = threading.Lock()

_events: "deque[dict]" = deque(maxlen=500)
_events_lock = threading.Lock()
_event_seq = 0
_drainer_started = False

_projects: Dict[str, dict] = {}
_projects_lock = threading.Lock()

# Live dashboard stats (never fetch network inside status_snapshot itself)
_quota_cache = {"remaining": None, "fetched_at": 0.0, "in_flight": False}
# Verified session state, refreshed in the background. `valid` is None until
# the first probe answers, so callers can tell "unknown" from "dead".
_session_cache = {"valid": None, "checked_at": 0.0, "in_flight": False}
_bid_count_cache = {"value": None}   # pre-start bid count, avoids a read per poll
_usd_cache = {"rate": None, "fetched_at": 0.0}
_started_at: Optional[int] = None  # epoch ms while monitor is running
_forex_provider = None
_forex_lock = threading.Lock()

# Busy flags for UI (set by views when spawning workers)
busy = {
    "starting": False,
    "stopping": False,
    "login": False,
    "verify": False,
    "bid": False,
    "test_ai": False,
    "test_tg": False,
    "demo": False,
}


def _serialize(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _enqueue(ev: dict) -> None:
    global _event_seq
    payload = {k: _serialize(v) for k, v in ev.items()}
    with _events_lock:
        _event_seq += 1
        rec = {"id": _event_seq, "type": payload.get("type", "unknown"), "payload": payload}
        _events.append(rec)
    if rec["type"] in ("login_done", "login_error"):
        busy["login"] = False
    # Cache projects for manual bid without re-fetching full dataclass
    if payload.get("type") == "new_project":
        project = payload.get("project") or {}
        pid = str(project.get("id") or "")
        if pid:
            with _projects_lock:
                _projects[pid] = project


def _start_drainer(app: Any) -> None:
    global _drainer_started
    if _drainer_started:
        return
    _drainer_started = True

    def loop():
        while True:
            try:
                ev = app.event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception:
                break
            if not isinstance(ev, dict):
                continue
            try:
                _enqueue(ev)
            except Exception as e:
                logger.debug(f"event serialize failed: {e}")

    threading.Thread(target=loop, daemon=True, name="botstate-events").start()


def get_app():
    """Lazily create the process-wide BotApp singleton."""
    global _app
    if _app is not None:
        return _app
    with _app_lock:
        if _app is not None:
            return _app
        # Windows consoles default to cp1252 — ConfigLoader.load() prints
        # Persian skill names and would raise UnicodeEncodeError under Django.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
        # Configure logging once (same idea as main.py) if not already set up
        root = logging.getLogger()
        if not root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(levelname)s - %(message)s",
                handlers=[
                    logging.FileHandler(
                        _log_path(), encoding="utf-8"
                    ),
                    logging.StreamHandler(),
                ],
            )
        from ponishabot.config import ConfigLoader
        from ponishabot.app import BotApp

        cfg = ConfigLoader.load()
        app = BotApp(cfg)
        app.event_queue = queue.Queue(maxsize=2000)
        _start_drainer(app)
        _app = app
        logger.info("BotApp singleton created (web GUI)")
        return _app


def _log_path() -> str:
    from ponishabot import paths
    return paths.LOG_PATH


def get_events(since: int = 0, limit: int = 200) -> List[dict]:
    with _events_lock:
        return [e for e in _events if e["id"] > since][:limit]


def latest_event_id() -> int:
    with _events_lock:
        return _event_seq


def get_projects() -> List[dict]:
    with _projects_lock:
        return sorted(
            _projects.values(),
            key=lambda p: p.get("approved_at") or 0,
            reverse=True,
        )


def get_project(pid: str) -> Optional[dict]:
    with _projects_lock:
        return _projects.get(str(pid))


def read_log_tail(since_byte: int = 0, max_bytes: int = 64_000) -> dict:
    """Return {since, text} of ponishabot.log from since_byte."""
    import os
    from ponishabot import paths
    try:
        if not os.path.exists(paths.LOG_PATH):
            return {"since": 0, "text": ""}
        size = os.path.getsize(paths.LOG_PATH)
        if size <= since_byte:
            return {"since": size, "text": ""}
        start = max(0, size - max_bytes, since_byte)
        with open(paths.LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            chunk = f.read()
        if start > 0 and "\n" in chunk:
            chunk = chunk.split("\n", 1)[1]
        return {"since": size, "text": chunk}
    except OSError as e:
        return {"since": since_byte, "text": f"[log read error: {e}]\n"}


def session_validity() -> "bool | None":
    """The last verified session verdict: True, False, or None if unknown.

    None is not the same as False — the probe makes a network call, so it has
    no answer yet on a fresh process. Callers must not render None as "dead".
    """
    return _session_cache["valid"]


def _session_probe_async() -> None:
    """Verify the stored session off-thread and cache the verdict.

    `has_cookies` only says a cookie exists — ponisha may already have revoked
    it, and this machine has exactly that. The dashboard read that flag as
    "ready to bid", so a dead session looked healthy. The probe makes a network
    call and so cannot run on the request path; the last verdict is served
    instead, and refreshed at most every 60s.
    """
    app = _app
    if app is None or _session_cache.get("in_flight"):
        return
    if (time.time() - _session_cache["checked_at"]) < 60:
        return
    _session_cache["in_flight"] = True

    def worker():
        try:
            _session_cache["valid"] = bool(app.has_valid_session())
        except Exception as e:
            logger.debug(f"session probe failed: {e}")
        finally:
            # Stamped on failure too: a probe that dies instantly (no network)
            # would otherwise be retried on every status poll, spawning a
            # thread every couple of seconds for nothing.
            _session_cache["checked_at"] = time.time()
            _session_cache["in_flight"] = False

    threading.Thread(target=worker, daemon=True, name="botstate-session").start()


def refresh_quota_async() -> None:
    """Fetch remaining proposals off-thread; store in cache + monitor if present."""
    app = _app
    if app is None or _quota_cache.get("in_flight"):
        return
    _quota_cache["in_flight"] = True

    def worker():
        try:
            remaining = app.get_remaining_proposals()
            _quota_cache["remaining"] = remaining
            _quota_cache["fetched_at"] = time.time()
            if remaining is not None and app.monitor is not None:
                app.monitor.remaining_proposals = remaining
            logger.info(f"Quota cache updated: {remaining}")
        except Exception as e:
            logger.warning(f"quota refresh failed: {e}")
            # allow retry after cooldown
            _quota_cache["fetched_at"] = 0.0
        finally:
            _quota_cache["in_flight"] = False

    threading.Thread(target=worker, daemon=True, name="botstate-quota").start()


def mark_started() -> None:
    global _started_at
    _started_at = int(time.time() * 1000)
    _bid_count_cache["value"] = None  # the monitor's set is authoritative now


def mark_stopped() -> None:
    global _started_at
    _started_at = None


def _get_forex():
    global _forex_provider
    with _forex_lock:
        if _forex_provider is None:
            from ponishabot.forex import ForexProvider
            source = "wallex"
            if _app is not None:
                source = getattr(_app.cfg, "forex_source", "wallex")
            _forex_provider = ForexProvider(source=source)
        return _forex_provider


def _usd_rate(app) -> "int | None":
    """Cached USD/Toman; refresh at most every 60s. None if forex disabled/fail."""
    import time as _t
    if app is None or not getattr(app.cfg, "forex_enabled", True):
        return None
    now = _t.time()
    if _usd_cache["rate"] is not None and (now - _usd_cache["fetched_at"]) < 60:
        return _usd_cache["rate"]

    def worker():
        try:
            rate = _get_forex().get_usd_toman()
        except Exception as e:
            logger.debug(f"usd refresh failed: {e}")
            return
        if rate:
            _usd_cache["rate"] = int(rate)
            _usd_cache["fetched_at"] = _t.time()

    if _usd_cache["rate"] is None or (now - _usd_cache["fetched_at"]) >= 60:
        threading.Thread(target=worker, daemon=True, name="botstate-usd").start()
    return _usd_cache["rate"]


def status_snapshot() -> dict:
    app = get_app()
    # Lazy quota: fetch once when never fetched; retry if still None after 30s
    now = time.time()
    if (
        not busy.get("starting")
        and not _quota_cache.get("in_flight")
        and (
            _quota_cache["fetched_at"] == 0.0
            or (
                _quota_cache["remaining"] is None
                and (now - _quota_cache["fetched_at"]) >= 30
            )
        )
    ):
        refresh_quota_async()

    _session_probe_async()

    remaining = None
    if app.monitor is not None and app.monitor.remaining_proposals is not None:
        remaining = app.monitor.remaining_proposals
    elif _quota_cache["remaining"] is not None:
        remaining = _quota_cache["remaining"]
    bids = 0
    if app.monitor is not None:
        bids = len(app.monitor.bid_log)
    else:
        # Before the bot starts there is no monitor, so fall back to the file.
        # Cache it: this runs on every /api/status poll (every 2s), and reading
        # the file each time re-logged "Loaded N persisted bid(s)" endlessly
        # and put a disk read on the request path for no reason.
        if _bid_count_cache["value"] is None:
            try:
                _bid_count_cache["value"] = len(app._load_bid_log())
            except Exception:
                _bid_count_cache["value"] = 0
        bids = _bid_count_cache["value"]
    cfg = app.cfg
    running = app.is_running()
    return {
        "running": running,
        "bidding": app.is_bidding(),
        "busy": dict(busy),
        "dry_run": bool(cfg.dry_run),
        "quota": remaining,
        "matches": len(_projects),
        "bids": bids,
        "usd": _usd_rate(app),
        "started_at": _started_at if running else None,
        "decision": type(app.decision).__name__ if app.decision else None,
        "ai_enabled": bool(cfg.ai_enabled),
        # A valid session is either stored cookies (browser login) or a Bearer
        # token from the OTP login flow — either one means we can talk to ponisha.
        # This is the "stored" half only; `session_valid` is the verified half
        # and is what the UI must use to decide whether bidding is possible.
        "has_cookies": bool(app.auth.get_cookies()) or bool(app.auth.get_access_token()),
        "session_valid": _session_cache["valid"],
        "refresh_interval": cfg.refresh_interval,
        "event_seq": latest_event_id(),
    }
