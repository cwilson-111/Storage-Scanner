"""An elevated Storage Scanner doesn't follow a junction a normal user made
(storage_scanner/redirection_guard.py). Each case runs in a child process:
the guard can't be turned off again in the process that turned it on."""

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="RedirectionGuard is Windows'"),
    # A junction made by an elevated test run is trusted (CI runners run as
    # an administrator), and the "unelevated" child would be elevated too.
    pytest.mark.skipif(
        sys.platform == "win32" and bool(ctypes.windll.shell32.IsUserAnAdmin()),
        reason="needs a test run without admin rights",
    ),
]
_CHILD = """
import ctypes, sys
sys.path.insert(0, sys.argv[1])
from storage_scanner import redirection_guard
if sys.argv[3] == "elevated":
    ctypes.windll.shell32.IsUserAnAdmin = lambda: 1  # as the --mft-scan helper is
print("guard", redirection_guard.guard_elevated_process())
try:
    with open(sys.argv[2], "a") as f:
        f.write("written through the junction")
    print("followed")
except OSError:
    print("refused")
"""


def _write_through_junction(tmp_path, mode):
    target = tmp_path / "System32-stand-in"
    target.mkdir()
    junction = tmp_path / "NeuralStorageMatrix"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], check=True)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(ROOT), str(junction / "storage_scanner.log"), mode],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.split(), (target / "storage_scanner.log").exists()


def test_an_elevated_process_refuses_a_junction_its_user_made(tmp_path):
    out, written = _write_through_junction(tmp_path, "elevated")

    if out[1] == "False":
        pytest.skip("this Windows has no RedirectionGuard (before Windows 11 22H2)")
    assert out == ["guard", "True", "refused"]
    assert not written


def test_an_unelevated_process_is_left_alone(tmp_path):
    out, written = _write_through_junction(tmp_path, "unelevated")

    assert out == ["guard", "False", "followed"]
    assert written
