"""ponishabot — terminal entry point.

Web UI:  python manage.py bot        → http://127.0.0.1:8000/
Terminal: python main.py             → monitor in this console (no desktop GUI)
Login:    python manage.py bot_login → open Chrome for ponisha.ir login

The desktop CustomTkinter GUI was replaced by the Django dashboard.
"""

import argparse
import logging
import sys

# Ensure UTF-8 output (especially when stdout is piped/redirected)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from ponishabot.paths import LOG_PATH

from ponishabot import botstate
from ponishabot.notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(stream=sys.stdout),
    ],
)

# Force UTF-8 on the console stream handler so Persian logs don't crash on
# Windows consoles that default to cp1252/latin-1.
try:
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
            h.stream.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    import logging as _logging

    _orig_emit = _logging.StreamHandler.emit

    def _safe_emit(self, record):
        try:
            msg = self.format(record)
            self.stream.write(msg + self.terminator)
        except UnicodeEncodeError:
            record.msg = str(record.msg).encode("utf-8", "replace").decode("utf-8", "replace")
            _orig_emit(self, record)
        except Exception:
            _orig_emit(self, record)

    _logging.StreamHandler.emit = _safe_emit

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="ponishabot — terminal monitor for ponisha.ir")
    parser.add_argument("--no-gui", action="store_true",
                        help="kept for compatibility; terminal is always used")
    parser.add_argument("--plain", action="store_true",
                        help="stream the log instead of the interactive panel")
    parser.add_argument("--log-level", choices=("important", "info", "debug"),
                        default="important",
                        help="how much the panel shows (default: important)")
    args = parser.parse_args()

    # Use the process-wide singleton rather than a fresh BotApp. A second
    # instance would carry its own Selenium driver and its own monitor while
    # botstate.status_snapshot() reported on the first one, so the panel would
    # show "stopped" with no quota while the bot was actually running. The web
    # dashboard has always gone through get_app() for the same reason.
    app = botstate.get_app()
    cfg = app.cfg
    TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id).notify_startup(
        "DRY_RUN" if cfg.dry_run else "LIVE")

    app.run_terminal(plain=args.plain, level=args.log_level)


if __name__ == "__main__":
    main()
