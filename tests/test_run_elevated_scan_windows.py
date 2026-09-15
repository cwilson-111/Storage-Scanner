"""Tests for storage_scanner.file_ops.run_elevated_scan_windows against a
faked ctypes.windll.{shell32,kernel32} -- the same technique already used
by tests/test_elevation.py for relaunch_elevated_windows.

Each fake Win32 "function" below is its own small class instance (not a
bound method) specifically so it tolerates having `.restype`/`.argtypes`
assigned to it, exactly like the real ctypes function objects the
production code configures.
"""

import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage_scanner.file_ops as file_ops
from storage_scanner.file_ops import run_elevated_scan_windows

_FAKE_PROCESS_HANDLE = 777


class _FakeShellExecuteExW:
    def __init__(self, succeed=True, process_handle=_FAKE_PROCESS_HANDLE):
        self.succeed = succeed
        self.process_handle = process_handle
        self.restype = None
        self.argtypes = None
        self.calls = []

    def __call__(self, info_ref):
        info = ctypes.cast(info_ref, ctypes.POINTER(file_ops._SHELLEXECUTEINFOW)).contents
        self.calls.append({
            "lpVerb": info.lpVerb, "lpFile": info.lpFile,
            "lpParameters": info.lpParameters, "nShow": info.nShow,
        })
        if not self.succeed:
            return 0
        info.hProcess = self.process_handle
        return 1


class _FakeWaitForSingleObject:
    def __init__(self, results):
        self.results = list(results)
        self.restype = None
        self.calls = []

    def __call__(self, handle, timeout_ms):
        self.calls.append((handle, timeout_ms))
        if self.results:
            return self.results.pop(0)
        return 0  # WAIT_OBJECT_0


class _FakeGetExitCodeProcess:
    def __init__(self, exit_code):
        self.exit_code = exit_code
        self.calls = 0

    def __call__(self, handle, exit_code_ref):
        self.calls += 1
        ctypes.cast(exit_code_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = self.exit_code
        return 1


class _FakeTerminateProcess:
    def __init__(self):
        self.calls = []

    def __call__(self, handle, exit_code):
        self.calls.append(handle)
        return 1


class _FakeCloseHandle:
    def __init__(self):
        self.calls = []

    def __call__(self, handle):
        self.calls.append(handle)
        return 1


class _FakeKernel32:
    def __init__(self, exit_code=0, wait_results=(0,)):
        self.WaitForSingleObject = _FakeWaitForSingleObject(wait_results)
        self.GetExitCodeProcess = _FakeGetExitCodeProcess(exit_code)
        self.TerminateProcess = _FakeTerminateProcess()
        self.CloseHandle = _FakeCloseHandle()


class _FakeShell32:
    def __init__(self, shell_execute_ex_w):
        self.ShellExecuteExW = shell_execute_ex_w


class _FakeWinDLL:
    def __init__(self, shell32, kernel32):
        self.shell32 = shell32
        self.kernel32 = kernel32


def _patch(monkeypatch, shell_execute_ex_w, kernel32):
    monkeypatch.setattr(
        ctypes, "windll",
        _FakeWinDLL(_FakeShell32(shell_execute_ex_w), kernel32),
        raising=False,
    )


def test_successful_scan_returns_parsed_json_and_cleans_up_the_temp_file(monkeypatch, tmp_path):
    # Give mkstemp a known, predictable path instead of parsing it back out
    # of the quoted command line -- simpler and doesn't depend on
    # subprocess.list2cmdline's quoting rules.
    output_path = tmp_path / "mft_scan_result.json"
    fd = os.open(str(output_path), os.O_CREAT | os.O_WRONLY)
    monkeypatch.setattr(file_ops.tempfile, "mkstemp", lambda **kwargs: (fd, str(output_path)))

    class _WritingShellExecuteExW(_FakeShellExecuteExW):
        def __call__(self, info_ref):
            result = super().__call__(info_ref)
            # Stand in for the real elevated helper actually writing its result.
            output_path.write_text(json.dumps({"path": "C:\\Data", "size": 42}), encoding="utf-8")
            return result

    shell_exec = _WritingShellExecuteExW(succeed=True)
    kernel32 = _FakeKernel32(exit_code=0, wait_results=[0])
    _patch(monkeypatch, shell_exec, kernel32)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    ok, result = run_elevated_scan_windows("C:\\Data", threading.Event())

    assert ok is True
    assert result == {"path": "C:\\Data", "size": 42}
    assert not output_path.exists()  # cleaned up
    assert kernel32.CloseHandle.calls == [_FAKE_PROCESS_HANDLE]

    call = shell_exec.calls[0]
    assert call["lpVerb"] == "runas"
    assert "--mft-scan" in call["lpParameters"]
    assert "--subtree" in call["lpParameters"]
    assert "C:\\Data" in call["lpParameters"]
    assert "--output" in call["lpParameters"]


def test_declined_elevation_returns_false_with_a_message(monkeypatch):
    shell_exec = _FakeShellExecuteExW(succeed=False)
    kernel32 = _FakeKernel32()
    _patch(monkeypatch, shell_exec, kernel32)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    ok, message = run_elevated_scan_windows("C:\\Data", threading.Event())

    assert ok is False
    assert "cancelled" in message.lower() or "failed" in message.lower()
    assert kernel32.CloseHandle.calls == []  # no process was ever created


def test_nonzero_exit_code_returns_false_with_a_message(monkeypatch):
    shell_exec = _FakeShellExecuteExW(succeed=True)
    kernel32 = _FakeKernel32(exit_code=1, wait_results=[0])
    _patch(monkeypatch, shell_exec, kernel32)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    ok, message = run_elevated_scan_windows("C:\\Data", threading.Event())

    assert ok is False
    assert "exited with code 1" in message


def test_missing_output_file_is_a_failure_not_a_crash(monkeypatch):
    # ShellExecuteExW "succeeds" and the process "exits" 0, but nothing
    # ever writes the output file -- simulates a helper that crashed
    # after spawning but produced no result.
    shell_exec = _FakeShellExecuteExW(succeed=True)
    kernel32 = _FakeKernel32(exit_code=0, wait_results=[0])
    _patch(monkeypatch, shell_exec, kernel32)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    ok, message = run_elevated_scan_windows("C:\\Data", threading.Event())

    assert ok is False
    assert "could not read" in message.lower()


def test_cancellation_before_the_process_exits_terminates_it(monkeypatch):
    shell_exec = _FakeShellExecuteExW(succeed=True)
    # Never signal WAIT_OBJECT_0 on its own -- only cancellation should end this.
    kernel32 = _FakeKernel32(exit_code=0, wait_results=[])
    _patch(monkeypatch, shell_exec, kernel32)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    cancel_event = threading.Event()
    cancel_event.set()

    ok, message = run_elevated_scan_windows("C:\\Data", cancel_event)

    assert ok is False
    assert message == "Cancelled."
    assert kernel32.TerminateProcess.calls == [_FAKE_PROCESS_HANDLE]
    assert kernel32.CloseHandle.calls == [_FAKE_PROCESS_HANDLE]


def test_frozen_build_uses_bare_executable_in_the_command_line(monkeypatch):
    shell_exec = _FakeShellExecuteExW(succeed=False)  # fail fast; only args matter here
    kernel32 = _FakeKernel32()
    _patch(monkeypatch, shell_exec, kernel32)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    run_elevated_scan_windows("C:\\Data", threading.Event())

    params = shell_exec.calls[0]["lpParameters"]
    assert sys.executable not in params
    assert "--mft-scan" in params
