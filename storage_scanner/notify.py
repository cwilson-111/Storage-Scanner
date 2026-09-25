"""Desktop notifications for headless runs -- today, a scheduled scan that
finds its folder over budget (cli.py `--notify`).

Standard library only, like the rest of the app, so each platform goes
through a tool it already ships:
- Windows: a toast through Windows PowerShell 5.1's WinRT bridge. An
  unpackaged app has no AppUserModelID of its own, so the toast is posted
  under PowerShell's and shows "Windows PowerShell" as its source.
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

import os
import shutil
import subprocess
from xml.sax.saxutils import escape

from storage_scanner.formatting import human_size
from storage_scanner.platform_support import IS_LINUX, IS_MACOS, IS_WINDOWS

TOAST_XML_ENV = "STORAGE_SCANNER_TOAST_XML"
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_TIMEOUT_SECONDS = 30

_WINRT_TYPE = ", ContentType = WindowsRuntime] > $null"
_WINDOWS_TOAST_SCRIPT = "\n".join(
    [
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications"
        + _WINRT_TYPE,
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument" + _WINRT_TYPE,
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        f"$xml.LoadXml($env:{TOAST_XML_ENV})",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{POWERSHELL_APP_ID}').Show($toast)",
    ]
)


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
    return (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        "</binding></visual></toast>"
    )


def windows_command():
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _WINDOWS_TOAST_SCRIPT,
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


def windows_toasts_enabled():
    """False when the user has turned notifications off for their whole
    account (Settings > System > Notifications). Windows then accepts a
    toast and silently drops it, so this is the only way to tell."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
        ) as key:
            value, _type = winreg.QueryValueEx(key, "ToastEnabled")
    except OSError:
        return True  # never set: Windows' default is on
    return value != 0


def notify(title, body):
    """Show a desktop notification. Returns (ok, error_message); never raises."""
    env = None
    creationflags = 0
    if IS_WINDOWS:
        if not windows_toasts_enabled():
            return False, "Windows notifications are turned off (Settings > System > Notifications)"
        command = windows_command()
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
    return True, ""
