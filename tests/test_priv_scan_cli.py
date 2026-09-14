import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.priv_scan_cli import run_priv_scan


def test_run_priv_scan_prints_scanned_tree_as_json(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world!")

    run_priv_scan(str(tmp_path))

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["is_dir"] is True
    assert data["file_count"] == 2
    assert data["size"] == len("hello") + len("world!")


def test_run_priv_scan_exits_nonzero_on_scan_failure(monkeypatch, capsys):
    def fake_scan(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("storage_scanner.priv_scan_cli.scan", fake_scan)

    with pytest.raises(SystemExit) as exc_info:
        run_priv_scan("/some/path")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "permission denied" in captured.err
