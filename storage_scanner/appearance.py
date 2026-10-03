"""Light or dark, and sharp on high-DPI screens.

Settings ▸ Appearance stores "system", "light" or "dark" (SETTING_KEY in
history_db's app_metadata); "system" follows the operating system's own
setting. The palettes themselves are settings.LIGHT_COLORS and DARK_COLORS.
A change applies the next time the app starts: every window takes its
colours when it's built.

enable_dpi_awareness runs before the first Tk window. Without it, Windows
draws the app at 96 DPI and stretches the bitmap on a 125-150% display,
which blurs every letter. It declares *system* DPI awareness, not
per-monitor: Tk 8.6 doesn't redraw at a new DPI when a window moves to
another monitor, so on a second monitor with a different scale Windows
stretching it is the better outcome. Tk then sizes fonts for the real DPI
and settings.px scales the pixel sizes to match.
"""

import ctypes
import subprocess

from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_LINUX, IS_MACOS, IS_WINDOWS

SETTING_KEY = "appearance"
SYSTEM, LIGHT, DARK = "system", "light", "dark"
CHOICES = ((SYSTEM, "Match System"), (LIGHT, "Light"), (DARK, "Dark"))

# Windows 10 1809+ and 11: DWMWA_USE_IMMERSIVE_DARK_MODE. Builds before
# 20H1 used 19 for the same thing.
_DWMWA_USE_IMMERSIVE_DARK_MODE = (20, 19)


def os_prefers_dark():
    """Whether the operating system is set to dark mode; False when that
    can't be read."""
    try:
        if IS_WINDOWS:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            with key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if IS_MACOS:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return result.stdout.strip() == "Dark"
        if IS_LINUX:
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return "dark" in result.stdout
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not read the system's light/dark setting", exc_info=True)
    return False


def resolve(setting):
    """LIGHT or DARK for a stored setting (anything unknown is SYSTEM)."""
    if setting in (LIGHT, DARK):
        return setting
    return DARK if os_prefers_dark() else LIGHT


def enable_dpi_awareness():
    """Declare system DPI awareness (Windows; nothing elsewhere). Must run
    before the first window exists. Returns whether it took effect."""
    if not IS_WINDOWS:
        return False
    try:
        # PROCESS_SYSTEM_DPI_AWARE; fails harmlessly if already declared.
        return ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        try:  # Windows 7/8.0: no shcore
            return bool(ctypes.windll.user32.SetProcessDPIAware())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            logger.debug("Could not declare DPI awareness", exc_info=True)
            return False


def use_dark_title_bar(window):
    """Give a mapped Tk window a dark title bar (Windows 10 1809+; nothing
    elsewhere or on older Windows)."""
    if not IS_WINDOWS:
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())  # type: ignore[attr-defined]
        on = ctypes.c_int(1)
        for attribute in _DWMWA_USE_IMMERSIVE_DARK_MODE:
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
                hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)
            )
            if result == 0:
                return
    except (AttributeError, OSError):
        logger.debug("Could not set a dark title bar", exc_info=True)
