"""Recycle Bin / Trash deletion and elevated-relaunch support."""

import ctypes
import os
import shlex
import subprocess
import sys
from ctypes import wintypes

from storage_scanner.platform_support import IS_MACOS


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # the bit that routes deletes to the Recycle Bin
_FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),   # FILEOP_FLAGS is a WORD
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _recycle_windows(path):
    """Send a file or folder to the Windows Recycle Bin (so it's recoverable).

    Uses the shell's SHFileOperationW with FOF_ALLOWUNDO — pure stdlib, no
    extra dependency. `pFrom` must be double-NUL terminated. Returns True on
    success, False otherwise.
    """
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = os.path.abspath(path) + "\x00\x00"
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


def _recycle_macos(path):
    """Move a file or folder to the macOS Trash via Finder (recoverable).

    Uses `osascript` to ask Finder to delete the path — pure stdlib, no
    extra dependency (send2trash, pyobjc, etc. not required). Finder resolves
    the name-collision itself if something with the same name is already in
    the Trash.
    """
    posix_path = os.path.abspath(path).replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{posix_path}"'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
    )
    return result.returncode == 0


def recycle(path):
    """Send a file or folder to the platform Recycle Bin / Trash (recoverable).

    Returns True on success, False otherwise.
    """
    if IS_MACOS:
        return _recycle_macos(path)
    return _recycle_windows(path)


def relaunch_elevated_macos(initial_path=None):
    """Relaunch this app as root via the native macOS admin-password prompt.

    Uses `osascript ... with administrator privileges`, which shows the
    standard system authorization dialog — no bundled helper tool or extra
    dependency required. The new process is started detached (backgrounded
    inside the privileged shell command) so this call returns as soon as
    the user approves or cancels the prompt.

    Returns True if the elevated process was launched (the caller should
    exit so only one instance is scanning), False if the user cancelled the
    prompt or authorization otherwise failed.
    """
    if getattr(sys, "frozen", False):
        args = [sys.executable]
    else:
        args = [sys.executable, os.path.abspath(sys.argv[0])]
    if initial_path:
        args.append(initial_path)

    shell_cmd = " ".join(shlex.quote(a) for a in args) + " > /dev/null 2>&1 &"
    escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = f'do shell script "{escaped}" with administrator privileges'

    result = subprocess.run(["osascript", "-e", apple_script], capture_output=True)
    return result.returncode == 0


def relaunch_elevated_windows(initial_path=None):
    """Relaunch this app elevated via the Windows UAC consent prompt.

    Uses ShellExecuteW's "runas" verb — pure stdlib (ctypes), no extra
    dependency or bundled manifest required. Windows itself starts the new
    process, so this call returns as soon as the user approves or dismisses
    the UAC dialog.

    Returns True if Windows accepted the elevation request (the caller
    should exit so only one instance is scanning), False if the user
    declined the prompt or it otherwise failed.
    """
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
    ]

    if getattr(sys, "frozen", False):
        target = sys.executable
        args = [initial_path] if initial_path else []
    else:
        target = sys.executable
        args = [os.path.abspath(sys.argv[0])]
        if initial_path:
            args.append(initial_path)

    params = subprocess.list2cmdline(args)
    # SW_SHOWNORMAL = 1. Per MSDN, a return value > 32 means success; the
    # small values below that are error codes (e.g. the user declining UAC).
    result = shell32.ShellExecuteW(None, "runas", target, params, None, 1)
    return int(result) > 32
