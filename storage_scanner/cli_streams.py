"""Somewhere for `--cli` to write when the program has no console.

The Windows exe is built `--windowed`, so Windows starts it without a
console. Typed into cmd.exe or PowerShell it gets no standard handles, and
Python sets sys.stdout and sys.stderr to None: writing the scan's JSON
raised AttributeError, and every message printed to stderr vanished (or,
worse, went into a redirected stdout, since `print(file=None)` means
stdout). A console program would simply share its shell's console, so this
attaches to that console and writes there.

Only the streams that are missing are replaced: output the caller
redirected to a pipe or a file stays where it was sent. With no console to
attach to either (Task Scheduler, a shortcut), stderr's messages go to the
app log instead, and stdout stays None for run_cli to report.
"""

import ctypes
import io
import os
import sys
from ctypes import wintypes

_ATTACH_PARENT_PROCESS = 0xFFFFFFFF  # (DWORD)-1
_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def connect_std_streams():
    """Point a missing sys.stdout or sys.stderr at the starting shell's
    console, or stderr at the app log when there is no console."""
    if sys.stdout is not None and sys.stderr is not None:
        return

    console = _open_parent_console()

    if sys.stdout is None and console is not None:
        sys.stdout = console
    if sys.stderr is None:
        sys.stderr = console if console is not None else _LogStream()


def _open_parent_console():
    """Attaches to the console of the process that started this one and
    returns a text stream writing to it, or None if there isn't one."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.AttachConsole.restype = wintypes.BOOL

    if not kernel32.AttachConsole(_ATTACH_PARENT_PROCESS):
        # The one-file exe runs as two processes: the one the shell started
        # unpacks the app, runs it as a child and waits. That first one has
        # no console either (it's the same windowed exe), so the shell's
        # console belongs to the grandparent. A venv's pythonw.exe starts
        # the real pythonw.exe the same way.
        processes = _processes()
        parent_pid, parent_name = processes.get(os.getpid(), (None, None))
        own_name = os.path.basename(sys.executable).lower()
        if parent_pid in processes and parent_name == own_name:
            kernel32.AttachConsole(processes[parent_pid][0])

    # Succeeds if an attach did, or if this process already had a console;
    # fails when there's no console at all.
    try:
        return open("CONOUT$", "w", encoding="utf-8", errors="backslashreplace")
    except OSError:
        return None


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _processes():
    """{pid: (parent pid, exe file name in lower case)} for every running
    process, or {} if Windows won't list them."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    entry_ptr = ctypes.POINTER(_PROCESSENTRY32W)
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, entry_ptr]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, entry_ptr]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, _INVALID_HANDLE_VALUE):
        return {}
    processes = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            processes[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


class _LogStream(io.TextIOBase):
    """A write-only text stream that sends each line to the app log."""

    def __init__(self):
        super().__init__()
        self._pending = ""
        self._writing = False

    def writable(self):
        return True

    def write(self, text):
        self._pending += text
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._log(line)
        return len(text)

    def flush(self):
        if self._pending:
            self._log(self._pending)
            self._pending = ""

    def _log(self, line):
        # The logger's own stderr fallback (no writable log folder) would
        # write straight back here; drop the line instead of recursing.
        if self._writing or not line.strip():
            return
        from storage_scanner.logging_setup import logger

        self._writing = True
        try:
            logger.info("--cli: %s", line)
        finally:
            self._writing = False
