"""Load settings from config.yaml"""

import logging
import os
from typing import Any, Dict

import yaml

from ponishabot import paths

logger = logging.getLogger(__name__)


class Config:
    """Typed view of the settings — handed to other modules."""

    def __init__(self, data: Dict[str, Any]):
        self._data = data

        telegram = data.get("telegram", {}) or {}
        # Telegram credentials: env var takes precedence over config file
        self.telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", telegram.get("bot_token", ""))
        self.telegram_chat_id: str = str(os.getenv("TELEGRAM_CHAT_ID", telegram.get("chat_id", "")))

        search = data.get("search", {})
        self.search_skills: list = search.get("skills", [])

        monitor = data.get("monitor", {})
        self.refresh_interval: int = monitor.get("refresh_interval", 300)
        self.max_pages: int = monitor.get("max_pages", 24)
        self.priority_window_minutes: int = monitor.get("priority_window_minutes", 5)

        bid = data.get("bid", {})
        self.budget_min: int = bid.get("budget_min", 0)
        self.budget_max: int = bid.get("budget_max", 10_000_000)
        self.proposal_template: str = bid.get("proposal_template", "descriptions.txt")
        self.min_skill_match: float = bid.get("min_skill_match", 0.5)
        self.dry_run: bool = bid.get("dry_run", True)
        self.min_remaining_proposals: int = int(bid.get("min_remaining_proposals", 2))

        competitive = bid.get("competitive_pricing", {})
        self.high_competition_threshold: int = competitive.get("high_competition_threshold", 15)
        self.urgency_premium_percent: int = competitive.get("urgency_premium_percent", 20)
        self.premium_badge_multiplier: float = competitive.get("premium_badge_multiplier", 1.15)
        self.low_competition_discount_percent: int = competitive.get("low_competition_discount_percent", 10)
        self.high_competition_discount_percent: int = competitive.get("high_competition_discount_percent", 15)

        browser = data.get("browser", {})
        self.headless: bool = browser.get("headless", False)
        self.show_browser: bool = browser.get("show_browser", False)
        self.user_data_dir: str = browser.get("user_data_dir", None)

        ai = data.get("ai", {})
        self.ai_enabled: bool = bool(ai.get("enabled", False))
        self.ai_base_url: str = ai.get("base_url", "")
        self.ai_api_key: str = ai.get("api_key", "")
        self.ai_model: str = ai.get("model", "")
        self.ai_min_skill_gate: float = float(ai.get("min_skill_gate", 0.2))
        self.ai_max_description_chars: int = int(ai.get("max_description_chars", 4000))
        self.ai_temperature: float = float(ai.get("temperature", 0.2))
        self.karlancer_enabled: bool = bool(ai.get("karlancer_enabled", False))
        self.karlancer_max_pages: int = int(ai.get("karlancer_max_pages", 2))

        # Forex / market calibration
        forex = data.get("forex", {})
        self.forex_enabled: bool = bool(forex.get("enabled", True))
        self.baseline_usd_rate: int = int(forex.get("baseline_usd_rate", 500000))
        self.forex_sensitivity: float = float(forex.get("sensitivity", 0.5))
        self.forex_source: str = forex.get("source", "wallex")

        # Karlancer market calibration (soft clamp)
        karl = data.get("karlancer", {})
        self.karlancer_soft_clamp_enabled: bool = bool(karl.get("soft_clamp_enabled", True))
        self.karlancer_clamp_multiplier: float = float(karl.get("clamp_multiplier", 3.0))
        self.karlancer_target_multiplier: float = float(karl.get("target_multiplier", 2.5))

        _profile = data.get("profile", {})
        self.profile_name: str = _profile.get("name", "")
        self.profile_about: str = _profile.get("about", "")
        self.profile_hourly_min: int = int(_profile.get("hourly_min", 0))
        self.profile_hourly_max: int = int(_profile.get("hourly_max", 0))

    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def to_dict(self) -> Dict[str, Any]:
        """Return settings as a dict for saving back."""
        return {
            "telegram": {
                "bot_token": self.telegram_token,
                "chat_id": self.telegram_chat_id,
            },
            "search": {
                "skills": self.search_skills,
            },
            "monitor": {
                "refresh_interval": self.refresh_interval,
                "max_pages": self.max_pages,
                "priority_window_minutes": self.priority_window_minutes,
            },
            "bid": {
                "budget_min": self.budget_min,
                "budget_max": self.budget_max,
                "proposal_template": self.proposal_template,
                "min_skill_match": self.min_skill_match,
                "dry_run": self.dry_run,
                "min_remaining_proposals": self.min_remaining_proposals,
                "competitive_pricing": {
                    "high_competition_threshold": self.high_competition_threshold,
                    "urgency_premium_percent": self.urgency_premium_percent,
                    "premium_badge_multiplier": self.premium_badge_multiplier,
                    "low_competition_discount_percent": self.low_competition_discount_percent,
                    "high_competition_discount_percent": self.high_competition_discount_percent,
                },
            },
            "browser": {
                "headless": self.headless,
                "show_browser": self.show_browser,
                "user_data_dir": self.user_data_dir,
            },
            "ai": {
                "enabled": self.ai_enabled,
                "base_url": self.ai_base_url,
                "api_key": self.ai_api_key,
                "model": self.ai_model,
                "min_skill_gate": self.ai_min_skill_gate,
                "max_description_chars": self.ai_max_description_chars,
                "temperature": self.ai_temperature,
                "karlancer_enabled": self.karlancer_enabled,
                "karlancer_max_pages": self.karlancer_max_pages,
            },
            "forex": {
                "enabled": self.forex_enabled,
                "baseline_usd_rate": self.baseline_usd_rate,
                "sensitivity": self.forex_sensitivity,
                "source": self.forex_source,
            },
            "karlancer": {
                "soft_clamp_enabled": self.karlancer_soft_clamp_enabled,
                "clamp_multiplier": self.karlancer_clamp_multiplier,
                "target_multiplier": self.karlancer_target_multiplier,
            },
            "profile": {
                "name": self.profile_name,
                "about": self.profile_about,
                "hourly_min": self.profile_hourly_min,
                "hourly_max": self.profile_hourly_max,
            },
        }


def _printer():
    """Return a print() that survives a console which cannot encode the text."""
    def _p(msg: str) -> None:
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode("utf-8", "replace").decode("ascii", "replace"))
    return _p


class ConfigLoader:
    @staticmethod
    def load(path: str = paths.CONFIG_PATH, quiet: bool = None) -> Config:
        """Read config.yaml, optionally printing a summary of what was loaded.

        The summary is suppressed when `quiet` is true, or when the
        PONISHABOT_QUIET_CONFIG environment variable is set. The TUI sets that
        variable before the app is built: the banner is a bare print() to
        stdout, so inside the rendered screen it appears mid-interface and
        pushes the layout up. `--plain` and the web dashboard keep it.
        """
        if quiet is None:
            quiet = bool(os.environ.get("PONISHABOT_QUIET_CONFIG"))
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = Config(data)
        if not quiet:
            # Encode-safe on cp1252 consoles (Persian skill names would
            # otherwise crash Windows terminals / Django).
            _p = _printer()
            _p("-" * 40)
            _p("Settings loaded:")
            _p(f"  telegram enabled: {cfg.has_telegram()}")
            _p(f"  search skills: {cfg.search_skills}")
            _p(f"  poll interval: {cfg.refresh_interval}s")
            _p(f"  budget: {cfg.budget_min}-{cfg.budget_max} | min_skill_match: {cfg.min_skill_match}")
            _p(f"  DRY_RUN: {cfg.dry_run} | headless: {cfg.headless}")
            _p(f"  AI: {'ON' if cfg.ai_enabled else 'off'} "
               f"({cfg.ai_model or 'no model'})")
            _p("-" * 40)
        return cfg

    @staticmethod
    def save(cfg: "Config", path: str = paths.CONFIG_PATH) -> None:
        """Persist settings to the yaml file (GUI edits become permanent)."""
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg.to_dict(), f, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
        logger.info(f"Settings saved to {path}")
