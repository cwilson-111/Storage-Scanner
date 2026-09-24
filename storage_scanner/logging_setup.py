"""Rotating file logging + crash diagnostics.

A windowed/one-file PyInstaller build has no console, so anything not
written to a file is simply lost. This sets up a rotating log file and
installs hooks so unhandled exceptions (main thread, background threads,
and Tk widget callbacks) always leave a trace for diagnosing a bug report.
"""

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from history import APP_DATA_DIR

LOG_DIR_ENV_VAR = "STORAGE_SCANNER_LOG_DIR"


def setup_logging():
    logger = logging.getLogger("storage_scanner")
    if logger.handlers:  # already configured (e.g. re-imported in tests)
        return logger
    logger.setLevel(logging.DEBUG)

    # STORAGE_SCANNER_LOG_DIR overrides where logs go. The test suite sets it
    # (tests/conftest.py) so test runs never write into the real app log;
    # this handler is attached at import time, before any fixture could
    # redirect it.
    log_dir = Path(os.environ.get(LOG_DIR_ENV_VAR) or APP_DATA_DIR / "logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "storage_scanner.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        # Can't write logs at all (read-only volume, locked-down profile,
        # etc.) — fall back to stderr so diagnostics aren't lost entirely.
        logger.addHandler(logging.StreamHandler())

    def _log_unhandled_exception(exc_type, exc_value, exc_tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_unhandled_exception

    def _log_thread_exception(args):
        logger.critical(
            "Unhandled exception in thread %r",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _log_thread_exception

    return logger


logger = setup_logging()
logger.info("Storage Scanner starting (pid=%s, platform=%s)", os.getpid(), sys.platform)
