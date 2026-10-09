"""Terminal UI for ponishabot.

`run_tui` is the entry point. It refuses to crash on a terminal that cannot host
a full-screen app: without `textual`, or with stdout piped to a file, it falls
back to streaming the log — the old behaviour — and says which happened.
"""

import logging
import sys
import time

logger = logging.getLogger(__name__)


def can_run_tui() -> bool:
    """Whether this environment can host the full-screen UI.

    Checked before any authentication so a piped run never opens a browser it
    has no way to show — the failure that made `python main.py > log.txt` crash
    on a host with no display.
    """
    try:
        import textual  # noqa: F401
    except ImportError:
        logger.warning("textual is not installed — falling back to plain log "
                       "output. Install it with: pip install textual")
        return False

    if not sys.stdout.isatty():
        logger.info("stdout is not a terminal — streaming logs instead of the TUI")
        return False

    return True


def run_tui(app, level: str = "important") -> int:
    """Run the TUI. Caller must have checked `can_run_tui()` first."""
    if not can_run_tui():
        return run_plain(app)

    if not app.build_monitor():
        logger.error("could not prepare the bot (authentication failed)")
        return 1

    from ponishabot.tui.app import PonishaTUI
    from ponishabot.tui.screens import ALL_SCREENS

    PonishaTUI.SCREEN_CLASSES = list(ALL_SCREENS)
    tui = PonishaTUI(app, level=level)
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if app.is_running():
                app.stop()
        except Exception as e:
            logger.warning(f"stop after TUI exit failed: {type(e).__name__}: {e}")
    return 0


def run_plain(app) -> int:
    """Stream the log, as `main.py` did before the TUI existed.

    The monitor is only started once the bot is ready. `app.start()` runs the
    whole auth chain, and on a host with no stored session that opens Chrome —
    which then dies with "tab crashed" on a machine that cannot show a window,
    leaving the process sitting on a dead driver instead of logging in the
    terminal. Preparing the monitor first surfaces that as a clear message.
    """
    if not app.is_running():
        # Auth can raise rather than return False: on a headless host the
        # browser itself fails ("tab crashed") and `manual_login` lets that
        # exception through. Catching it here turns an unhandled traceback into
        # a message naming the way out, without changing the auth flow the web
        # dashboard depends on.
        try:
            ready = app.build_monitor()
        except Exception as e:
            logger.error(f"could not prepare the bot: {type(e).__name__}: {e}")
            ready = False
        if not ready:
            logger.error("not authenticated — run `python manage.py bot_login` "
                         "once on a machine with a display, or use the OTP login "
                         "in the web UI")
            return 1
        app.start_monitor_thread()
    try:
        while app.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0
