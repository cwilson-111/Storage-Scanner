import csv
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.cli import EXIT_OK, EXIT_SCAN_ERROR, run_cli


def test_missing_path_argument_exits_2_via_argparse(capsys):
    with pytest.raises(SystemExit) as exc_info:
        run_cli([])
    assert exc_info.value.code == 2


def test_bad_format_choice_exits_2_via_argparse(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        run_cli([str(tmp_path), "--format", "xml"])
    assert exc_info.value.code == 2


def test_nonexistent_path_returns_scan_error_exit_code(capsys):
    code = run_cli(["/definitely/does/not/exist/anywhere"])
    assert code == EXIT_SCAN_ERROR
    captured = capsys.readouterr()
    assert "does not exist" in captured.err


def test_json_output_to_stdout_is_clean(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello")

    code = run_cli([str(tmp_path)])

    assert code == EXIT_OK
    captured = capsys.readouterr()
    # stdout must be pure JSON (a human summary goes to stderr instead),
    # or a pipe like `--cli . | jq` would break on the extra text.
    data = json.loads(captured.out)
    assert data["file_count"] == 1
    assert data["size"] == len("hello")
    assert "Scanned" in captured.err


def test_csv_output_to_stdout(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world!")

    code = run_cli([str(tmp_path), "--format", "csv"])

    assert code == EXIT_OK
    captured = capsys.readouterr()
    reader = csv.DictReader(io.StringIO(captured.out))
    rows = list(reader)
    names = {row["name"] for row in rows}
    assert "a.txt" in names
    assert "b.txt" in names
    assert "sub" in names


def test_output_to_file(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello")
    out_file = tmp_path / "result.json"

    code = run_cli([str(tmp_path), "--output", str(out_file)])

    assert code == EXIT_OK
    data = json.loads(out_file.read_text())
    assert data["file_count"] == 1
    # Nothing but the stderr summary should have gone to the terminal.
    captured = capsys.readouterr()
    assert captured.out == ""


def test_unwritable_output_path_returns_scan_error(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello")
    bad_output = tmp_path / "no_such_dir" / "result.json"

    code = run_cli([str(tmp_path), "--output", str(bad_output)])

    assert code == EXIT_SCAN_ERROR
    captured = capsys.readouterr()
    assert "Could not write output file" in captured.err


def test_format_none_writes_no_data(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello")

    code = run_cli([str(tmp_path), "--format", "none"])

    assert code == EXIT_OK
    assert capsys.readouterr().out == ""


def test_save_history_records_the_scan_even_on_a_fresh_database(tmp_path, monkeypatch, capsys):
    import history
    from storage_scanner.scan_history import normalize_scan_path

    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    scanned = tmp_path / "scanned"
    scanned.mkdir()
    (scanned / "a.txt").write_text("hello")

    code = run_cli([str(scanned), "--save-history", "--format", "none"])

    assert code == EXIT_OK
    assert "Saved to scan history" in capsys.readouterr().err
    assert history.get_latest_scan_id(normalize_scan_path(str(scanned))) is not None


def test_save_history_failure_returns_scan_error(tmp_path, monkeypatch, capsys):
    from storage_scanner import scan_history

    def broken(_node):
        raise OSError("disk is full")

    monkeypatch.setattr(scan_history, "record_scan", broken)
    (tmp_path / "a.txt").write_text("hello")

    code = run_cli([str(tmp_path), "--save-history", "--format", "none"])

    assert code == EXIT_SCAN_ERROR
    assert "disk is full" in capsys.readouterr().err


def _over_budget_folder(tmp_path, monkeypatch):
    import history
    from storage_scanner.scan_history import normalize_scan_path

    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()
    scanned = tmp_path / "scanned"
    scanned.mkdir()
    (scanned / "a.txt").write_text("x" * 100)
    history.set_budget(normalize_scan_path(str(scanned)), 10)
    return scanned


def test_notify_shows_a_notification_when_over_budget(tmp_path, monkeypatch, capsys):
    from storage_scanner import notify

    scanned = _over_budget_folder(tmp_path, monkeypatch)
    shown = []
    monkeypatch.setattr(notify, "notify", lambda title, body: shown.append(body) or (True, ""))

    code = run_cli([str(scanned), "--save-history", "--notify", "--format", "none"])

    assert code == EXIT_OK
    assert len(shown) == 1
    assert str(scanned) in shown[0]


def test_no_notification_without_the_flag(tmp_path, monkeypatch, capsys):
    from storage_scanner import notify

    scanned = _over_budget_folder(tmp_path, monkeypatch)
    shown = []
    monkeypatch.setattr(notify, "notify", lambda title, body: shown.append(body) or (True, ""))

    run_cli([str(scanned), "--save-history", "--format", "none"])

    assert shown == []
    assert "Over budget" in capsys.readouterr().err


def test_failed_notification_is_reported_but_scan_still_succeeds(tmp_path, monkeypatch, capsys):
    from storage_scanner import notify

    scanned = _over_budget_folder(tmp_path, monkeypatch)
    monkeypatch.setattr(notify, "notify", lambda title, body: (False, "no D-Bus session"))

    code = run_cli([str(scanned), "--save-history", "--notify", "--format", "none"])

    assert code == EXIT_OK
    assert "no D-Bus session" in capsys.readouterr().err
