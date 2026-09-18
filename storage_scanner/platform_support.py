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
IS_LINUX = sys.platform.startswith("linux")


def _detect_elevated():
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            logger.warning("IsUserAnAdmin() check failed", exc_info=True)
            return False
    # Covers macOS and Linux (and any other POSIX) alike -- os.geteuid()
    # doesn't exist on Windows at all, so this branch is never reached
    # there. Previously gated on IS_MACOS specifically, which meant this
    # always returned False on Linux even when actually run as root via
    # sudo -- the same check is equally valid on any POSIX platform.
    return hasattr(os, "geteuid") and os.geteuid() == 0


IS_ROOT = _detect_elevated()

if IS_MACOS:
    FILE_MANAGER_NAME = "Finder"
elif IS_WINDOWS:
    FILE_MANAGER_NAME = "Explorer"
else:
    FILE_MANAGER_NAME = "Files"  # generic term, matches GNOME Files/Nautilus/Dolphin etc.

TRASH_NAME = "Recycle Bin" if IS_WINDOWS else "Trash"  # matches the XDG Trash spec's own naming on Linux
