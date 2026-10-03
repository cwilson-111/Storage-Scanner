"""Desktop notifications for headless runs -- today, a scheduled scan that
finds its folder over budget (cli.py `--notify`).

Standard library only, like the rest of the app, so each platform goes
through a tool it already ships:
- Windows: a toast through Windows PowerShell 5.1's WinRT bridge, posted
  under this app's own AppUserModelID. An unpackaged app has none unless
  it registers one; a per-user registry key (the way Firefox's installer
  does it) gives the toast the name "Storage Scanner" and the app icon,
  with no Start-menu shortcut or installer. notify() writes that key
  before every toast; removing the last scheduled scan deletes it again
  (unregister_windows_app_id). check_windows_toasts() asks Windows whether
  it would show one, without posting it (the Schedule Scans window says).
  PowerShell 7 (pwsh) dropped the WinRT bridge, so powershell.exe is
  called explicitly.
- macOS: `osascript` `display notification`.
- Linux: `notify-send` (libnotify). Under cron it usually fails, because
  cron doesn't pass the desktop session's D-Bus address; the failure is
  reported rather than hidden.

User text (folder paths) never becomes code: the toast XML travels in an
environment variable and is escaped as XML, and osascript/notify-send get
it as plain argv items.
"""

import contextlib
import os
import shutil
import struct
import subprocess
from pathlib import Path

from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_LINUX, IS_MACOS, IS_WINDOWS, resource_path

TOAST_XML_ENV = "STORAGE_SCANNER_TOAST_XML"
# The winget package identifier: unique to this app, and stable.
APP_ID = "cwilson-111.StorageScanner"
APP_DISPLAY_NAME = "Storage Scanner"
APP_ID_KEY = "Software\\Classes\\AppUserModelId\\" + APP_ID
ICON_FILE_NAME = "notification-icon.png"
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
SETTINGS_APP_ID = (
    "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel"
)
_TIMEOUT_SECONDS = 30

# Windows accepts a toast it won't show without any error and drops it, so
# the script asks ToastNotifier.Setting first and prints it; the toast
# script then posts only when that's Enabled or unknown. An app ID has
# no setting until its first toast (reading it throws "Element not found");
# the account-wide and Group Policy switches apply to it all the same, and
# Windows' own apps report those: PowerShell, and Settings, which is always
# installed. Their per-app switches are their own, so those don't count.
_WINRT_TYPE = ", ContentType = WindowsRuntime] > $null"
_WINDOWS_SETTING_SCRIPT = "\n".join(
    [
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications"
        + _WINRT_TYPE,
        "function Get-ToastSetting($id) {",
        "  try { [string][Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier($id).get_Setting() } catch { '' }",
        "}",
        f"$setting = Get-ToastSetting '{APP_ID}'",
        f"foreach ($id in '{POWERSHELL_APP_ID}', '{SETTINGS_APP_ID}') {{",
        "  if ($setting) { break }",
        "  $setting = Get-ToastSetting $id",
        "  if ($setting -eq 'DisabledForApplication') { $setting = '' }",
        "}",
        "[Console]::Out.Write($setting)",
    ]
)
_WINDOWS_TOAST_SCRIPT = "\n".join(
    [
        _WINDOWS_SETTING_SCRIPT,
        "if ($setting -and $setting -ne 'Enabled') { exit }",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument" + _WINRT_TYPE,
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        f"$xml.LoadXml($env:{TOAST_XML_ENV})",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{APP_ID}').Show($toast)",
    ]
)

# What each blocking ToastNotifier.Setting means, and where it's turned back on.
_WINDOWS_BLOCKED_REASONS = {
    "DisabledForUser": (
        "Windows notifications are turned off for this account "
        "(Settings > System > Notifications)"
    ),
    "DisabledForApplication": (
        f"notifications from {APP_DISPLAY_NAME} are turned off "
        f"(Settings > System > Notifications > {APP_DISPLAY_NAME})"
    ),
    "DisabledByGroupPolicy": "notifications are turned off by Group Policy on this PC",
    "DisabledByManifest": "Windows has notifications turned off for this app",
}


def budget_breach_message(display_path, breach):
    """(title, body) for one budgets.BudgetBreach. `display_path` is the
    path as the user typed it; breach.path is the normalized
    (lower-cased on Windows) history key."""
    return (
        "Storage Scanner: over budget",
        f"{display_path} is {human_size(breach.current_size_bytes)}, "
        f"over its {human_size(breach.threshold_bytes)} budget.",
    )


def windows_toast_xml(title, body):
    from xml.sax.saxutils import escape  # here, not at startup: it pulls in urllib

    return (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        "</binding></visual></toast>"
    )


def windows_command(script):
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]


def macos_command(title, body):
    return [
        "osascript",
        "-e",
        "on run argv",
        "-e",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e",
        "end run",
        title,
        body,
    ]


def linux_command(title, body):
    return ["notify-send", "--app-name=Storage Scanner", title, body]


def windows_blocked_reason(setting):
    """Why Windows won't show this app's toasts, for the ToastNotifier.Setting
    the script printed; "" when it will, or when Windows didn't say."""
    if setting in ("", "Enabled"):
        return ""
    return _WINDOWS_BLOCKED_REASONS.get(
        setting, f"Windows won't show notifications for this app ({setting})"
    )


def _icon_copy_path():
    from storage_scanner.history_db import APP_DATA_DIR

    return APP_DATA_DIR / ICON_FILE_NAME


def _largest_png(ico):
    """The largest image in an .ico whose images are PNGs, as make_icon.py
    (Pillow) writes them. Raises ValueError or struct.error otherwise."""
    count = struct.unpack_from("<H", ico, 4)[0]
    # Each 16-byte directory entry: width (0 means 256), ..., size, offset.
    entries = [struct.unpack_from("<B7xII", ico, 6 + 16 * index) for index in range(count)]
    _width, size, offset = max(entries, key=lambda entry: entry[0] or 256)
    png = ico[offset : offset + size]
    if not png.startswith(b"\x89PNG"):
        raise ValueError("the icon's largest image isn't a PNG")
    return png


def _place_icon():
    """The app icon as a PNG in the app-data folder, for the toast: Windows
    shows no icon from an .ico there, and a one-file build's bundled files
    are deleted when the scan exits. Returns its path, or None."""
    target = _icon_copy_path()
    try:
        png = _largest_png(Path(resource_path("icon.ico")).read_bytes())
        if not (target.exists() and target.read_bytes() == png):
            target.write_bytes(png)
    except (OSError, ValueError, struct.error) as exc:
        logger.warning("Couldn't place the app icon for notifications: %s", exc)
    return str(target) if target.exists() else None


def register_windows_app_id():
    """Give APP_ID's toasts the app's name and icon. Windows reads both
    from this per-user key when it shows a toast. Raises OSError."""
    import winreg

    icon = _place_icon()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_ID_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY_NAME)
        if icon:
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon)


def unregister_windows_app_id():
    """Remove what register_windows_app_id wrote; anything already gone is
    fine. Raises OSError."""
    import winreg

    with contextlib.suppress(FileNotFoundError):
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, APP_ID_KEY)
    with contextlib.suppress(FileNotFoundError):
        _icon_copy_path().unlink()


def _run(command, env=None, creationflags=0):
    """(True, stdout) or (False, what went wrong); never raises."""
    try:
        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    # PowerShell can print an error and still exit 0, so stderr counts too.
    error = (result.stderr or "").strip()
    if result.returncode != 0 or error:
        return False, error or f"{command[0]} exited with code {result.returncode}"
    return True, (result.stdout or "").strip()


def check_windows_toasts():
    """Whether Windows will show this app's toasts, asked without posting
    one. Returns (True, ""), (False, why not and where to turn them back
    on), or (None, why Windows couldn't be asked). Never raises."""
    ok, output = _run(
        windows_command(_WINDOWS_SETTING_SCRIPT), creationflags=subprocess.CREATE_NO_WINDOW
    )
    if not ok:
        return None, output
    reason = windows_blocked_reason(output)
    return not reason, reason


def notify(title, body):
    """Show a desktop notification. Returns (ok, error_message); never raises."""
    env = None
    creationflags = 0
    if IS_WINDOWS:
        try:
            register_windows_app_id()
        except OSError as exc:
            # The toast still shows, just under the bare app ID.
            logger.warning("Couldn't register the notification app name: %s", exc)
        command = windows_command(_WINDOWS_TOAST_SCRIPT)
        env = {**os.environ, TOAST_XML_ENV: windows_toast_xml(title, body)}
        creationflags = subprocess.CREATE_NO_WINDOW
    elif IS_MACOS:
        command = macos_command(title, body)
    elif IS_LINUX:
        if shutil.which("notify-send") is None:
            return False, "notify-send is not installed (package: libnotify-bin / libnotify)"
        command = linux_command(title, body)
    else:
        return False, "desktop notifications aren't supported on this platform"

    ok, output = _run(command, env, creationflags)
    if not ok:
        return False, output
    # On Windows the script prints the setting it checked before posting.
    reason = windows_blocked_reason(output) if IS_WINDOWS else ""
    return not reason, reason
