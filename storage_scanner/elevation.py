"""Running a scan, or the whole app, with administrator rights.

On Windows the app relaunches itself through the UAC prompt, or runs Turbo
Scan's headless --mft-scan helper elevated; on macOS and Linux only a
headless --priv-scan runs as root (each function says why).
"""

import contextlib
import ctypes
import json
import os
import shlex
import subprocess
import sys
import tempfile
from ctypes import wintypes

from storage_scanner.drive_info import get_volume_root
from storage_scanner.logging_setup import logger
from storage_scanner.scan_progress import Phase

PHASE_WAITING_FOR_ELEVATION = "Waiting for administrator approval"
PHASE_LOADING_RESULTS = "Loading the Turbo Scan results"

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
        ("hIconOrMonitor", wintypes.HANDLE),  # a union in the real struct;
        # neither member is used here
        ("hProcess", wintypes.HANDLE),
    ]


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

    result = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
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
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
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


def _relay_progress_file(progress_path, progress_q, last_phase):
    """Read the elevated helper's --progress-file (see mft_scan_cli.
    _ProgressFileWriter: the current Phase as JSON) and, if it changed
    since `last_phase`, post it to `progress_q` as a ("phase", Phase) --
    exactly what the in-process Turbo Scan path posts itself, so the
    progress panel can't tell the two apart.

    Returns the (possibly unchanged) last phase to pass into the next
    call. Never raises: a missing file (helper hasn't started/written
    yet), an empty file (mid-write on the other end, despite the writer
    side's own atomic swap -- cheap extra safety), or unparseable
    content are all just "nothing new yet," not errors.
    """
    if progress_q is None:
        return last_phase
    try:
        with open(progress_path, encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            return last_phase
        phase = Phase.from_dict(json.loads(text))
    except (OSError, ValueError, KeyError, TypeError):
        return last_phase
    if phase != last_phase:
        progress_q.put(("phase", phase))
    return phase


def run_elevated_scan_windows(path, progress_q, cancel_event):
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

    A second temp file (--progress-file) is polled on the same cadence,
    relaying the helper's current step and its count into `progress_q` via
    _relay_progress_file -- without this, a scan running through this
    elevated-helper path (the common case: anyone who hasn't already
    launched the whole GUI as admin) posted zero progress of any kind for
    its entire duration, often 15s-60s+ on a cold scan, indistinguishable
    from a hang. A real bug, found via user report, not something this
    project's own validation had exercised (earlier real-hardware
    validation of this same helper always invoked it directly from an
    already-elevated terminal, bypassing the actual ShellExecuteExW/UAC
    GUI flow entirely).

    Polls with WaitForSingleObject in a short timeout loop rather than
    blocking outright, so `cancel_event` can be honored: if it's set
    mid-wait, the elevated process is terminated outright -- safe, since
    Turbo Scan only ever performs read-only volume reads. (The existing
    macOS elevated path has no cancellation support at all; this is
    already strictly better, even best-effort.)

    Returns (True, parsed_dict) on success, (False, error_message) on any
    failure: elevation declined, the helper exiting non-zero (with the
    helper's own error, when it wrote one -- see _helper_failure), a
    missing or unparseable output file, or cancellation.
    """
    volume_root = get_volume_root(path)

    fd, output_path = tempfile.mkstemp(prefix="mft_scan_", suffix=".json")
    os.close(fd)  # only the path is wanted -- the elevated child opens it itself
    fd, progress_path = tempfile.mkstemp(prefix="mft_scan_progress_", suffix=".txt")
    os.close(fd)

    try:
        if getattr(sys, "frozen", False):
            target = sys.executable
            args = [
                "--mft-scan",
                volume_root,
                "--subtree",
                path,
                "--output",
                output_path,
                "--progress-file",
                progress_path,
            ]
        else:
            target = sys.executable
            args = [
                os.path.abspath(sys.argv[0]),
                "--mft-scan",
                volume_root,
                "--subtree",
                path,
                "--output",
                output_path,
                "--progress-file",
                progress_path,
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
        _post_phase(progress_q, PHASE_WAITING_FOR_ELEVATION)
        succeeded = shell32.ShellExecuteExW(ctypes.byref(info))
        if not succeeded or not info.hProcess:
            return False, "Authorization was cancelled or failed."

        h_process = info.hProcess
        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        last_phase = None
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
                last_phase = _relay_progress_file(progress_path, progress_q, last_phase)

            exit_code = wintypes.DWORD(0)
            kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
        finally:
            kernel32.CloseHandle(h_process)

        if exit_code.value != 0:
            return False, _helper_failure(output_path, exit_code.value)

        # Reading a whole volume's result back (and turbo_scan's
        # dict_to_node after it) takes seconds on a big tree.
        _post_phase(progress_q, PHASE_LOADING_RESULTS)
        try:
            with open(output_path, encoding="utf-8") as f:
                return True, json.load(f)
        except (OSError, ValueError) as exc:
            return False, f"Could not read Turbo Scan result: {exc}"
    finally:
        with contextlib.suppress(OSError):
            os.remove(output_path)
        # The progress writer's temp file too, left behind when its last
        # swap never got past the reader (see mft_scan_cli._ProgressFileWriter).
        for leftover in (progress_path, progress_path + ".tmp"):
            with contextlib.suppress(OSError):
                os.remove(leftover)


def _helper_failure(output_path, exit_code):
    """The failure message for a helper that exited with `exit_code`: its
    own error, when it managed to write one to the output file (see
    mft_scan_cli._write_error), with the traceback logged here -- the
    helper's stderr is lost under ShellExecuteExW."""
    message = f"Turbo Scan helper exited with code {exit_code}"
    try:
        with open(output_path, encoding="utf-8") as f:
            envelope = json.load(f)
        error = envelope["error"]
    except (OSError, ValueError, KeyError, TypeError):
        return message + "."
    if envelope.get("traceback"):
        logger.warning("Turbo Scan helper failed:\n%s", envelope["traceback"])
    return f"{message}: {error}"


def _post_phase(progress_q, label):
    if progress_q is not None:
        progress_q.put(("phase", Phase(label)))
