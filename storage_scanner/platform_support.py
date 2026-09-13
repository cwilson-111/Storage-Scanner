"""Platform detection, elevation status, and OS-specific naming.

Everything here is read at import time so the rest of the app can branch
on IS_WINDOWS/IS_MACOS/IS_ROOT as plain booleans.
"""

import ctypes
import os
import sys

from storage_scanner.logging_setup import logger


def resource_path(name):
    """Resolve a bundled resource, whether running from source or a
    PyInstaller one-file build (which unpacks data into sys._MEIPASS)."""
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        # This module lives in storage_scanner/, one level below the
        # project root where the entry script and icon.ico actually live.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def _detect_elevated():
    if IS_MACOS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            logger.warning("IsUserAnAdmin() check failed", exc_info=True)
            return False
    return False


IS_ROOT = _detect_elevated()

FILE_MANAGER_NAME = "Finder" if IS_MACOS else "Explorer"
TRASH_NAME = "Trash" if IS_MACOS else "Recycle Bin"
