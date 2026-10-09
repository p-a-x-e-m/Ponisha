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
    """Run the TUI. Caller must have checked `can_run_tui()` first.

    Authentication is deliberately not done here. `build_monitor()` signs in,
    and with no stored session that opens Chrome and blocks on `input()` from
    the main thread — which is the thread the TUI needs to render on, so the
    interface never appeared and the first thing a new user saw was a login
    prompt in a terminal. Signing in happens from the Login screen instead.
    """
    if not can_run_tui():
        return run_plain(app)

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

    Authentication is deliberately not done here either. `build_monitor()` signs
    in, and with no stored session that opens Chrome and blocks on `input()` —
    the opposite of streaming the log, and on a host with no display it ends in
    a "tab crashed". A run that cannot sign in says so and exits, rather than
    opening a window the operator did not ask for.
    """
    if not app.is_running():
        # A stored cookie is not proof of a live session — ponisha may have
        # invalidated it, and on this machine that is exactly the case. Without
        # the probe the monitor gets built, Chrome opens, and the run blocks on
        # input() in a mode documented as streaming the log.
        try:
            valid = app.has_valid_session()
        except Exception as e:
            logger.debug(f"session probe failed: {type(e).__name__}: {e}")
            valid = False
        if not valid:
            logger.error("not authenticated — open the dashboard (`python "
                         "manage.py bot`) and sign in from the Login screen, or "
                         "run `python manage.py bot_login` once on a machine "
                         "with a display")
            return 1
        try:
            ready = app.build_monitor()
        except Exception as e:
            logger.error(f"could not prepare the bot: {type(e).__name__}: {e}")
            ready = False
        if not ready:
            logger.error("could not prepare the bot")
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
