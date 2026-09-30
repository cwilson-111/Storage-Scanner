"""Windows: an optional "Scan with Storage Scanner" entry on the right-click
menu of folders, drives and a folder's background (Settings ▸ Add to Folder
Right-Click Menu).

Written under HKEY_CURRENT_USER\\Software\\Classes, so it needs no
administrator rights and affects only this user. Windows 11 lists such
entries under "Show more options". The command is this copy of the app
(schedule.app_launch_args) plus the folder, which Storage-Scanner.py scans
straight away.
"""

import contextlib
import subprocess

from storage_scanner.schedule import app_launch_args, launch_location_problem

MENU_TEXT = "Scan with Storage Scanner"
_KEY_NAME = "StorageScanner"
# Where each kind of entry lives, and the placeholder Explorer fills in with
# the folder: %1 for a folder or drive that was clicked, %V for the folder
# whose background was.
_TARGETS = (
    (r"Directory\shell", "%1"),
    (r"Drive\shell", "%1"),
    (r"Directory\Background\shell", "%V"),
)
CLASSES_KEY = r"Software\Classes"


def menu_command(launch_args, placeholder):
    """The command line Explorer runs for the entry. The placeholder is
    quoted so a folder name with spaces stays one argument; a drive root
    then arrives as `C:"` (the backslash escapes the quote), which
    app.folder_argument repairs."""
    return f'{subprocess.list2cmdline(launch_args)} "{placeholder}"'


def is_installed(classes_key=CLASSES_KEY):
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, rf"{classes_key}\{_TARGETS[0][0]}\{_KEY_NAME}"
        ):
            return True
    except OSError:
        return False


def install(classes_key=CLASSES_KEY, launch_args=None):
    """Add the entries for this copy of the app. Raises ValueError (a
    message for the user) when this copy runs from somewhere temporary."""
    import winreg

    if launch_args is None:
        launch_args = app_launch_args()
        problem = launch_location_problem(launch_args)
        if problem:
            raise ValueError(problem)
    for target, placeholder in _TARGETS:
        base = rf"{classes_key}\{target}\{_KEY_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MENU_TEXT)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, launch_args[0])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, menu_command(launch_args, placeholder))


def uninstall(classes_key=CLASSES_KEY):
    """Remove the entries; missing ones are fine."""
    import winreg

    for target, _placeholder in _TARGETS:
        base = rf"{classes_key}\{target}\{_KEY_NAME}"
        for key_path in (rf"{base}\command", base):
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
