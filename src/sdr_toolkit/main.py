"""Entry point: logging, problem reports, then the window."""

import logging
import logging.handlers
import sys
import traceback
from pathlib import Path

from sdr_toolkit import APP_ID, APP_NAME, __version__, appreports, system


def state_dir() -> Path:
    return system.state_dir()


def _log_exception(exc_type, exc, tb) -> None:
    # Without a hook, PyQt6 aborts the process on an exception in a slot.
    traceback.print_exception(exc_type, exc, tb)
    logging.error("uncaught exception", exc_info=(exc_type, exc, tb))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        from sdr_toolkit.selftest import run as self_test
        sys.exit(self_test(sys.argv[2]))
    log_file = state_dir() / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(log_file, maxBytes=512 * 1024, backupCount=1)],
    )
    logging.info("%s %s starting", APP_NAME, __version__)
    system.setup_environment()
    sys.excepthook = _log_exception
    appreports.init(APP_ID, APP_NAME, __version__, state_dir(), log_file=log_file)

    from sdr_toolkit.gui import run_gui

    sys.exit(run_gui(state_dir()))


if __name__ == "__main__":
    main()
