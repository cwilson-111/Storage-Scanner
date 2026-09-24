import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner import budgets


def _init_db(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()
    return db_path


def _insert_scan(db_path, scan_path, total_size, created_at):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, "
        "folder_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (scan_path, total_size, 1_000_000, 10, 3, created_at),
    )
    conn.commit()
    conn.close()


def test_set_budget_then_list_and_delete(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)

    history.set_budget("/Users/me/Downloads", 50 * 1024**3)
    rows = history.list_budgets()

    assert len(rows) == 1
    budget_id, path, threshold_bytes, _created_at = rows[0]
    assert path == "/Users/me/Downloads"
    assert threshold_bytes == 50 * 1024**3

    history.delete_budget(budget_id)
    assert history.list_budgets() == []


def test_set_budget_upserts_same_path(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)

    history.set_budget("/Users/me/Downloads", 10)
    history.set_budget("/Users/me/Downloads", 20)

    rows = history.list_budgets()
    assert len(rows) == 1
    assert rows[0][2] == 20


def test_get_latest_scan_snapshot_returns_most_recent(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _insert_scan(db_path, "/Users/me/Downloads", 100, "2024-01-01T00:00:00")
    _insert_scan(db_path, "/Users/me/Downloads", 300, "2024-03-01T00:00:00")
    _insert_scan(db_path, "/Users/me/Downloads", 200, "2024-02-01T00:00:00")

    snapshot = history.get_latest_scan_snapshot("/Users/me/Downloads")

    assert snapshot[0] == "2024-03-01T00:00:00"
    assert snapshot[1] == 300


def test_get_latest_scan_snapshot_returns_none_when_never_scanned(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert history.get_latest_scan_snapshot("/never/scanned") is None


def test_check_all_budgets_skips_paths_with_no_scan_history(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    history.set_budget("/never/scanned", 100)

    assert budgets.check_all_budgets() == []


def test_check_all_budgets_flags_only_exceeded_budgets(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _insert_scan(db_path, "/Users/me/Downloads", 100 * 1024**3, "2024-01-01T00:00:00")
    _insert_scan(db_path, "/Users/me/Documents", 5 * 1024**3, "2024-01-01T00:00:00")

    history.set_budget("/Users/me/Downloads", 50 * 1024**3)  # exceeded
    history.set_budget("/Users/me/Documents", 50 * 1024**3)  # within budget

    breaches = budgets.check_all_budgets()

    assert len(breaches) == 1
    assert breaches[0].path == "/Users/me/Downloads"
    assert breaches[0].current_size_bytes == 100 * 1024**3


def test_check_all_budgets_marks_old_scans_as_stale(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    old_date = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    _insert_scan(db_path, "/Users/me/Downloads", 100, old_date)
    history.set_budget("/Users/me/Downloads", 10)

    breaches = budgets.check_all_budgets()

    assert len(breaches) == 1
    assert breaches[0].is_stale is True


def test_check_all_budgets_marks_recent_scans_as_not_stale(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _insert_scan(db_path, "/Users/me/Downloads", 100, recent)
    history.set_budget("/Users/me/Downloads", 10)

    breaches = budgets.check_all_budgets()

    assert len(breaches) == 1
    assert breaches[0].is_stale is False


def test_check_budget_for_path_returns_fresh_breach_when_exceeded(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    history.set_budget("/Users/me/Downloads", 50)

    breach = budgets.check_budget_for_path("/Users/me/Downloads", 100)

    assert breach is not None
    assert breach.current_size_bytes == 100
    assert breach.is_stale is False


def test_check_budget_for_path_returns_none_when_within_budget(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    history.set_budget("/Users/me/Downloads", 50)

    assert budgets.check_budget_for_path("/Users/me/Downloads", 10) is None


def test_check_budget_for_path_returns_none_when_no_budget_defined(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert budgets.check_budget_for_path("/Users/me/Downloads", 999_999) is None
