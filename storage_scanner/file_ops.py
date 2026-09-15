"""Recycle Bin / Trash deletion and elevated-relaunch support."""

import ctypes
import json
import os
import shlex
import subprocess
import sys
import tempfile
from ctypes import wintypes

from storage_scanner.drive_info import get_volume_root
from storage_scanner.platform_support import IS_MACOS


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # the bit that routes deletes to the Recycle Bin
_FOF_NOERRORUI = 0x0400

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_HIDE = 0
_WAIT_POLL_MS = 200
_WAIT_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0x00000000
_WAIT_FAILED = 0xFFFFFFFF


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),   # a union in the real struct;
                                                # neither member is used here
        ("hProcess", wintypes.HANDLE),
    ]


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


def open_trash():
    """Open the platform Recycle Bin / Trash in the file manager, so a user
    reading the audit log can go find/restore something themselves. Pure
    stdlib: Windows' Recycle Bin is a virtual shell folder, not a real
    filesystem path `os.startfile` can open directly.
    """
    try:
        if IS_MACOS:
            subprocess.run(["open", os.path.expanduser("~/.Trash")], check=True)
        else:
            subprocess.run(["explorer.exe", "shell:RecycleBinFolder"], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def run_elevated_scan_macos(path):
    """Scan `path` with root filesystem access via the admin-password prompt.

    Relaunching the *whole* GUI as root doesn't work on macOS: `do shell
    script ... with administrator privileges` runs the child through the
    Security framework's authorization trampoline, which detaches it from
    the caller's window-server connection as part of its privilege
    separation — a Tk window it tries to open there just never appears (an
    earlier attempt at this only got as far as discovering the process also
    got SIGHUP-killed once the auth session tore down; even after fixing
    that with `nohup`, the window still couldn't show, because losing the
    window-server connection is the deeper, unfixable-that-way problem).

    So only a *headless* scan (`--priv-scan`) ever runs as root: it walks
    the tree with elevated access and prints the resulting tree as JSON,
    which `do shell script` hands back as its own return value — no window
    needed. The still-running, still-visible GUI process (as the normal
    user) reads that JSON back and displays it like any other scan result.

    Returns (True, json_text) on success, (False, error_message) if
    authorization was cancelled/failed or the scan itself errored.
    """
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--priv-scan", path]
    else:
        args = [sys.executable, os.path.abspath(sys.argv[0]), "--priv-scan", path]

    quoted = " ".join(shlex.quote(a) for a in args)
    escaped = quoted.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = f'do shell script "{escaped}" with administrator privileges'

    result = subprocess.run(
        ["osascript", "-e", apple_script], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, (result.stderr or "Authorization was cancelled or failed.").strip()
    return True, result.stdout


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


def run_elevated_scan_windows(path, cancel_event):
    """Scan `path` with Turbo Scan's raw-volume access via a headless
    elevated helper process (`--mft-scan`, see
    storage_scanner/mft_scan_cli.py), while this (unprivileged) process
    keeps running.

    relaunch_elevated_windows's ShellExecuteW "runas" verb has no way to
    hand this process the elevated child's stdout -- the OS elevation
    broker calls CreateProcess for the new process, not us, so there's no
    pipe to attach, unlike macOS's `do shell script` (see
    run_elevated_scan_macos). Instead the helper writes its JSON result to
    a temp file this function creates and reads back once the process
    exits, using ShellExecuteExW's SEE_MASK_NOCLOSEPROCESS to get a real
    process handle to wait on.

    Polls with WaitForSingleObject in a short timeout loop rather than
    blocking outright, so `cancel_event` can be honored: if it's set
    mid-wait, the elevated process is terminated outright -- safe, since
    Turbo Scan only ever performs read-only volume reads. (The existing
    macOS elevated path has no cancellation support at all; this is
    already strictly better, even best-effort.)

    Returns (True, parsed_dict) on success, (False, error_message) on any
    failure: elevation declined, the helper exiting non-zero, a missing or
    unparseable output file, or cancellation.
    """
    volume_root = get_volume_root(path)

    fd, output_path = tempfile.mkstemp(prefix="mft_scan_", suffix=".json")
    os.close(fd)  # only the path is wanted -- the elevated child opens it itself

    try:
        if getattr(sys, "frozen", False):
            target = sys.executable
            args = ["--mft-scan", volume_root, "--subtree", path, "--output", output_path]
        else:
            target = sys.executable
            args = [
                os.path.abspath(sys.argv[0]), "--mft-scan", volume_root,
                "--subtree", path, "--output", output_path,
            ]
        params = subprocess.list2cmdline(args)

        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS
        info.hwnd = None
        info.lpVerb = "runas"
        info.lpFile = target
        info.lpParameters = params
        info.lpDirectory = None
        info.nShow = _SW_HIDE
        info.hProcess = None

        shell32 = ctypes.windll.shell32
        succeeded = shell32.ShellExecuteExW(ctypes.byref(info))
        if not succeeded or not info.hProcess:
            return False, "Authorization was cancelled or failed."

        h_process = info.hProcess
        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    kernel32.TerminateProcess(h_process, 1)
                    kernel32.WaitForSingleObject(h_process, _WAIT_INFINITE)
                    return False, "Cancelled."
                wait_result = kernel32.WaitForSingleObject(h_process, _WAIT_POLL_MS)
                if wait_result == _WAIT_OBJECT_0:
                    break
                if wait_result == _WAIT_FAILED:
                    return False, "Waiting for the Turbo Scan helper process failed."

            exit_code = wintypes.DWORD(0)
            kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
        finally:
            kernel32.CloseHandle(h_process)

        if exit_code.value != 0:
            return False, f"Turbo Scan helper exited with code {exit_code.value}."

        try:
            with open(output_path, "r", encoding="utf-8") as f:
                return True, json.load(f)
        except (OSError, ValueError) as exc:
            return False, f"Could not read Turbo Scan result: {exc}"
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass
