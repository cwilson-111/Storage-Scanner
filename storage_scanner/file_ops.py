"""Recycle Bin / Trash deletion and elevated-relaunch support."""

import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes
from datetime import datetime
from urllib.parse import quote

from storage_scanner.drive_info import get_volume_root
from storage_scanner.platform_support import IS_LINUX, IS_MACOS


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


def _xdg_trash_home():
    """The user's own home-volume XDG trash directory: ~/.local/share/Trash
    (or $XDG_DATA_HOME/Trash). Only correct for a path on the SAME
    filesystem as $HOME -- the XDG Trash spec calls for a per-mountpoint
    $topdir/.Trash-$uid instead for anything else (a removable drive, a
    separate /home mount, etc.), not implemented here. A known, stated
    scope limit rather than something silently gotten wrong -- matching
    how this project already documents similar single-case coverage
    elsewhere (e.g. mft_volume.py's single-contiguous-$MFT-extent note)."""
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg_data_home, "Trash")


def _recycle_linux_manual(path):
    """Move `path` into ~/.local/share/Trash per the XDG Trash spec,
    without depending on any trash-cli/gio tool being installed --
    _recycle_linux's fallback for a minimal/headless install that has
    neither. Only correct for paths on the same filesystem as $HOME (see
    _xdg_trash_home)."""
    trash_home = _xdg_trash_home()
    files_dir = os.path.join(trash_home, "files")
    info_dir = os.path.join(trash_home, "info")
    try:
        os.makedirs(files_dir, exist_ok=True)
        os.makedirs(info_dir, exist_ok=True)

        abs_path = os.path.abspath(path)
        name = os.path.basename(abs_path.rstrip(os.sep)) or abs_path
        # The spec requires a unique name within the trash; a plain
        # collision counter (matching Explorer's/Finder's own "file (2)"
        # convention) is enough -- two concurrent deletes racing for the
        # exact same free name is astronomically unlikely for a
        # single-user desktop tool.
        dest_name, suffix = name, 1
        while (os.path.exists(os.path.join(files_dir, dest_name))
               or os.path.exists(os.path.join(info_dir, dest_name + ".trashinfo"))):
            suffix += 1
            dest_name = f"{name}.{suffix}"

        info_path = os.path.join(info_dir, dest_name + ".trashinfo")
        with open(info_path, "w", encoding="utf-8") as f:
            f.write("[Trash Info]\n")
            f.write(f"Path={quote(abs_path, safe='/')}\n")
            f.write(f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n")

        shutil.move(abs_path, os.path.join(files_dir, dest_name))
        return True
    except OSError:
        return False


def _recycle_linux(path):
    """Move a file or folder to the Linux desktop Trash (recoverable).

    Prefers `gio trash` (part of glib2, present on most GNOME/GTK-based
    desktops -- Ubuntu, Fedora Workstation, etc.): it correctly defers to
    whatever trash implementation the user's actual desktop environment
    uses, including the separate per-mountpoint trash a removable/non-home
    filesystem needs. Falls back to a manual XDG Trash-spec move (see
    _recycle_linux_manual) if `gio` isn't installed or fails, so deletion
    still stays recoverable even on a minimal/headless install with no
    desktop trash tool at all. Never depends on a third-party package (no
    send2trash) per this project's zero-runtime-dependency policy.
    """
    try:
        result = subprocess.run(
            ["gio", "trash", os.path.abspath(path)], capture_output=True,
        )
        if result.returncode == 0:
            return True
    except OSError:
        pass  # gio not installed -- fall through to the manual implementation
    return _recycle_linux_manual(path)


def recycle(path):
    """Send a file or folder to the platform Recycle Bin / Trash (recoverable).

    Returns True on success, False otherwise.
    """
    if IS_MACOS:
        return _recycle_macos(path)
    if IS_LINUX:
        return _recycle_linux(path)
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
        elif IS_LINUX:
            try:
                # "trash:///" is the GVFS URI most file managers (Nautilus,
                # Nemo, ...) render as the proper, familiar Trash view --
                # nicer than the raw ~/.local/share/Trash/files folder,
                # which mixes deleted items in with the spec's own
                # .trashinfo metadata files.
                subprocess.run(["gio", "open", "trash:///"], check=True)
            except (OSError, subprocess.CalledProcessError):
                subprocess.run(["xdg-open", _xdg_trash_home()], check=True)
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


def run_elevated_scan_linux(path):
    """Scan `path` with root filesystem access via a PolicyKit (pkexec)
    prompt.

    Same headless-only shape as run_elevated_scan_macos, for a related but
    distinct reason: unlike macOS, `pkexec` genuinely *can* show a root-
    owned window on a traditional X11 session (several real Linux disk
    tools launch themselves this way) -- but Wayland compositors
    generally refuse a root process's connection to the user's session
    outright, as a hard security boundary, and there's no reliable way
    to tell from here which one a given user is actually running. A
    headless scan (`--priv-scan`, exactly the same entry point macOS
    already uses -- it's pure storage_scanner.scanner.scan() plus a JSON
    dump, nothing macOS-specific about it) sidesteps the question
    entirely: it never opens a window, so it works the same under X11,
    Wayland, or even a display-less SSH session with polkit configured.
    The still-running, still-visible GUI stays unprivileged throughout.

    pkexec is the de-facto standard for "ordinary GUI app needs root" on
    Linux -- ships by default with GNOME/KDE and most desktop distros,
    unlike bare `sudo`, which has no GUI password prompt of its own and
    would just hang waiting on a TTY that doesn't exist here. It needs a
    running polkit authentication agent to actually display that prompt.

    Confirmed on a real (agent-less) machine, not just assumed: contrary
    to what an earlier version of this docstring claimed, pkexec does
    NOT fail fast when no authentication agent is registered -- it just
    blocks indefinitely, waiting for a prompt response that can never
    arrive. `timeout=` below turns that into a bounded, reported failure
    instead of hanging this whole thread (and by extension the "Cancel"
    button, since nothing here currently threads a cancel_event through
    to pkexec) forever.

    Returns (True, json_text) on success, (False, error_message) if
    authorization was cancelled/failed, pkexec itself isn't installed,
    no authentication agent responded within the timeout, or the scan
    itself errored.
    """
    if getattr(sys, "frozen", False):
        args = ["pkexec", sys.executable, "--priv-scan", path]
    else:
        args = ["pkexec", sys.executable, os.path.abspath(sys.argv[0]), "--priv-scan", path]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except OSError as exc:
        return False, f"Could not run pkexec (is PolicyKit installed?): {exc}"
    except subprocess.TimeoutExpired:
        return False, (
            "Timed out waiting for authentication. This usually means no "
            "PolicyKit authentication agent is running for this desktop "
            "session (common on minimal window managers/headless setups) "
            "-- install one (e.g. polkit-gnome, lxqt-policykit, or your "
            "desktop's own) and try again."
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
