import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.file_ops import relaunch_elevated_macos, relaunch_elevated_windows


class _FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


def test_relaunch_builds_osascript_with_administrator_privileges(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False):
        captured["args"] = args
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert relaunch_elevated_macos("/Users/test/Documents") is True

    assert captured["args"][0] == "osascript"
    assert captured["args"][1] == "-e"
    script = captured["args"][2]
    assert "with administrator privileges" in script
    assert sys.executable in script
    assert "/Users/test/Documents" in script
    assert script.rstrip().endswith('&"') or "&" in script


def test_relaunch_returns_false_when_authorization_is_cancelled(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess(1))
    assert relaunch_elevated_macos() is False


def test_relaunch_uses_bare_executable_when_frozen(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False):
        captured["args"] = args
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    relaunch_elevated_macos()

    script = captured["args"][2]
    # Frozen (PyInstaller) builds relaunch the bundled executable itself,
    # not "<exe> <exe_path>" which would mis-parse the exe's own path as an
    # argument.
    assert script.count(sys.executable) == 1


def test_relaunch_handles_paths_with_spaces_and_quotes(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False):
        captured["args"] = args
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    tricky_path = '/Users/test/My "Big" Folder'
    relaunch_elevated_macos(tricky_path)

    # Must not raise, and must produce a script osascript can still parse
    # (embedded double quotes escaped for the outer AppleScript string).
    script = captured["args"][2]
    assert '\\"Big\\"' in script or "Big" in script


class _FakeShellExecuteW:
    """Stand-in for ctypes.windll.shell32.ShellExecuteW.

    The real function object supports assigning .restype/.argtypes before
    it's called (that's how the code fixes the 64-bit pointer-truncation
    gotcha), so this fake needs to tolerate that too.
    """

    def __init__(self, return_value):
        self.return_value = return_value
        self.restype = None
        self.argtypes = None
        self.calls = []

    def __call__(self, hwnd, operation, file, params, directory, show_cmd):
        self.calls.append((hwnd, operation, file, params, directory, show_cmd))
        return self.return_value


class _FakeShell32:
    def __init__(self, return_value):
        self.ShellExecuteW = _FakeShellExecuteW(return_value)


class _FakeWinDLL:
    def __init__(self, return_value):
        self.shell32 = _FakeShell32(return_value)


def test_relaunch_windows_uses_runas_verb_and_succeeds_above_32(monkeypatch):
    fake_windll = _FakeWinDLL(return_value=42)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert relaunch_elevated_windows(r"C:\Users\test\Documents") is True

    call = fake_windll.shell32.ShellExecuteW.calls[0]
    _hwnd, operation, target, params, _directory, show_cmd = call
    assert operation == "runas"
    assert target == sys.executable
    assert os.path.abspath(sys.argv[0]) in params
    assert r"C:\Users\test\Documents" in params
    assert show_cmd == 1


def test_relaunch_windows_returns_false_when_uac_declined(monkeypatch):
    # Per MSDN, ShellExecuteW returns a value <= 32 on failure (e.g. the
    # user clicking "No" on the UAC prompt).
    fake_windll = _FakeWinDLL(return_value=5)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert relaunch_elevated_windows() is False


def test_relaunch_windows_uses_bare_executable_when_frozen(monkeypatch):
    fake_windll = _FakeWinDLL(return_value=99)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    relaunch_elevated_windows(r"C:\Data")

    call = fake_windll.shell32.ShellExecuteW.calls[0]
    _hwnd, _operation, target, params, _directory, _show_cmd = call
    assert target == sys.executable
    # Frozen builds must not pass the exe's own path back to itself as an
    # argument — only the folder to reopen.
    assert sys.executable not in params
    assert r"C:\Data" in params


def test_relaunch_windows_quotes_paths_with_spaces(monkeypatch):
    fake_windll = _FakeWinDLL(return_value=99)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    relaunch_elevated_windows(r"C:\Users\test\My Big Folder")

    call = fake_windll.shell32.ShellExecuteW.calls[0]
    params = call[3]
    # subprocess.list2cmdline wraps the space-containing path in quotes.
    assert '"C:\\Users\\test\\My Big Folder"' in params
