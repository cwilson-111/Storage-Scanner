import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.file_ops import (
    relaunch_elevated_windows,
    run_elevated_scan_linux,
    run_elevated_scan_macos,
)


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_elevated_scan_builds_osascript_with_administrator_privileges(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False, text=False):
        captured["args"] = args
        return _FakeCompletedProcess(0, stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    # run_elevated_scan_macos only ever runs for real on macOS, where
    # sys.executable is a forward-slash POSIX path -- pinned here so this
    # test's own assertions hold regardless of which OS actually runs the
    # test suite. Left to the real host's sys.executable, this test would
    # fail on a Windows runner: a Windows sys.executable is backslash-
    # heavy, and the AppleScript-string escaping below doubles every
    # backslash (correct for an actual stray backslash in a real macOS
    # path), so the literal, un-doubled sys.executable would no longer
    # appear in the built script.
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    ok, output = run_elevated_scan_macos("/Users/test/Documents")

    assert ok is True
    assert output == '{"ok": true}'
    assert captured["args"][0] == "osascript"
    assert captured["args"][1] == "-e"
    script = captured["args"][2]
    assert "with administrator privileges" in script
    assert sys.executable in script
    assert "/Users/test/Documents" in script
    # Runs to completion in the foreground (no `&`/nohup): this is a
    # headless scan whose stdout `do shell script` needs to hand back, not
    # a GUI relaunch that has to survive past the auth session tearing down.
    assert "--priv-scan" in script
    assert "&" not in script
    assert "nohup" not in script


def test_elevated_scan_returns_false_when_authorization_is_cancelled(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(1, stderr="128:User canceled."),
    )
    ok, output = run_elevated_scan_macos("/Users/test")
    assert ok is False
    assert "canceled" in output.lower()


def test_elevated_scan_uses_bare_executable_when_frozen(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False, text=False):
        captured["args"] = args
        return _FakeCompletedProcess(0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # See test_elevated_scan_builds_osascript_with_administrator_privileges
    # for why this is pinned rather than left as the real host's own value.
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    run_elevated_scan_macos("/Users/test")

    script = captured["args"][2]
    # Frozen (PyInstaller) builds relaunch the bundled executable itself,
    # not "<exe> <exe_path>" which would mis-parse the exe's own path as an
    # argument.
    assert script.count(sys.executable) == 1


def test_elevated_scan_handles_paths_with_spaces_and_quotes(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False, text=False):
        captured["args"] = args
        return _FakeCompletedProcess(0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    tricky_path = '/Users/test/My "Big" Folder'
    run_elevated_scan_macos(tricky_path)

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


def test_elevated_scan_linux_builds_pkexec_command_with_priv_scan(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False, text=False, timeout=None):
        captured["args"] = args
        captured["timeout"] = timeout
        return _FakeCompletedProcess(0, stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    ok, output = run_elevated_scan_linux("/home/test/Documents")

    assert ok is True
    assert output == '{"ok": true}'
    args = captured["args"]
    assert args[0] == "pkexec"
    assert args[1] == sys.executable
    assert os.path.abspath(sys.argv[0]) in args
    assert "--priv-scan" in args
    assert "/home/test/Documents" in args
    # Must be bounded -- confirmed on a real agent-less machine that
    # pkexec blocks indefinitely rather than failing fast when no
    # PolicyKit authentication agent is running, unlike osascript on
    # macOS (which always has the OS's own auth UI available).
    assert captured["timeout"] is not None


def test_elevated_scan_linux_uses_bare_executable_when_frozen(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False, text=False, timeout=None):
        captured["args"] = args
        return _FakeCompletedProcess(0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    run_elevated_scan_linux("/home/test")

    args = captured["args"]
    assert args.count(sys.executable) == 1


def test_elevated_scan_linux_returns_false_when_authorization_is_cancelled(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(127, stderr="Not authorized"),
    )
    ok, output = run_elevated_scan_linux("/home/test")
    assert ok is False
    assert "not authorized" in output.lower()


def test_elevated_scan_linux_returns_false_when_pkexec_is_not_installed(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("no such file or directory: 'pkexec'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, output = run_elevated_scan_linux("/home/test")
    assert ok is False
    assert "pkexec" in output.lower()


def test_elevated_scan_linux_times_out_cleanly_instead_of_hanging_forever(monkeypatch):
    # Confirmed against a real machine with no PolicyKit authentication
    # agent registered: pkexec just blocks forever waiting for a prompt
    # response that can never come. This is the fix for that -- a bounded
    # subprocess.run(timeout=...) turned into a normal reported failure.
    def fake_run(args, capture_output=False, text=False, timeout=None):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, output = run_elevated_scan_linux("/home/test")
    assert ok is False
    assert "authentication" in output.lower() or "timed out" in output.lower()
