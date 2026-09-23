import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smoke_test_build import main, run_smoke_test


def test_a_missing_binary_fails(tmp_path):
    failures = run_smoke_test(tmp_path / "StorageScanner.exe")

    assert failures == [f"built binary exists: {tmp_path / 'StorageScanner.exe'}"]


def test_a_binary_that_is_not_the_app_fails_instead_of_passing_silently(capsys):
    # The Python interpreter rejects --cli with exit code 2: the smoke test
    # must report that, not mistake "it ran" for "it worked".
    failures = run_smoke_test(sys.executable)

    assert any("exit code 0" in f for f in failures)
    assert any("JSON result" in f for f in failures)


def test_main_exits_1_on_failure(tmp_path):
    assert main([str(tmp_path / "nothing-here")]) == 1
