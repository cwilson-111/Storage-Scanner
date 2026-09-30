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
from typing import Optional

from history import APP_DATA_DIR

LOG_DIR_ENV_VAR = "STORAGE_SCANNER_LOG_DIR"
# DEBUG for a diagnosis; INFO otherwise (DEBUG lines name every scanned path).
LOG_LEVEL_ENV_VAR = "STORAGE_SCANNER_LOG_LEVEL"
LOG_FILE_NAME = "storage_scanner.log"
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 3

# The folder the log file is written to, or None when logging fell back to
# stderr (see setup_logging). The error dialog's "Open Log Folder" opens it.
log_dir: Optional[Path] = None


def _rotate_at_start(path):
    """Start a new log file if this one is over _MAX_BYTES, keeping
    _BACKUP_COUNT old ones -- once, when a process starts, never while it
    runs. RotatingFileHandler renamed the file mid-run, and on Windows that
    fails while another process (the app and a scheduled scan) has it open,
    losing lines (2,593 of 30,000 in a two-process test). If a rename fails
    here, this process just appends to the current file."""
    try:
        if path.stat().st_size < _MAX_BYTES:
            return
        for index in range(_BACKUP_COUNT - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def _append_stream(path):
    """A text stream that only ever appends to `path`, whole writes at a
    time, even with other processes writing too. On POSIX that's O_APPEND
    ("a" mode). On Windows "a" mode only moves to the end before each
    write, so two processes overwrite each other's lines (1,778 of 30,000
    lost without any rotation); a handle opened for FILE_APPEND_DATA alone
    makes Windows itself append every write."""
    if sys.platform != "win32":
        return open(path, "a", encoding="utf-8")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # Its own instance, so the prototype set here doesn't change the shared
    # ctypes.windll.kernel32 other modules call.
    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.restype = wintypes.HANDLE
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    file_append_data, share_all, open_always, normal = 0x4, 0x7, 4, 0x80
    handle = create_file(str(path), file_append_data, share_all, None, open_always, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_BINARY)  # type: ignore[attr-defined]
    return open(fd, "a", encoding="utf-8")


def setup_logging():
    logger = logging.getLogger("storage_scanner")
    if logger.handlers:  # already configured (e.g. re-imported in tests)
        return logger
    level_name = os.environ.get(LOG_LEVEL_ENV_VAR, "").strip().upper()
    logger.setLevel(getattr(logging, level_name, None) or logging.INFO)

    # STORAGE_SCANNER_LOG_DIR overrides where logs go. The test suite sets it
    # (tests/conftest.py) so test runs never write into the real app log;
    # this handler is attached at import time, before any fixture could
    # redirect it.
    global log_dir
    folder = Path(os.environ.get(LOG_DIR_ENV_VAR) or APP_DATA_DIR / "logs")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _rotate_at_start(folder / LOG_FILE_NAME)
        handler = logging.StreamHandler(_append_stream(folder / LOG_FILE_NAME))
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        log_dir = folder
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
