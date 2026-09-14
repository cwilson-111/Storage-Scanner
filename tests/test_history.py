import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history


def test_get_latest_scan_id_returns_most_recent_scan(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))

    history.init_history_db()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Example", 100, 1000, 10, 3, "2024-01-01T00:00:00"),
    )
    cur.execute(
        """
        INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Example", 200, 1000, 20, 5, "2024-02-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    assert history.get_latest_scan_id("C:/Example") == 2


def test_list_scans_for_path_returns_all_scans_newest_first(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))

    history.init_history_db()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for total_size, created_at in [
        (100, "2024-01-01T00:00:00"),
        (200, "2024-02-01T00:00:00"),
        (150, "2024-03-01T00:00:00"),
    ]:
        cur.execute(
            """
            INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("C:/Example", total_size, 1000, 10, 3, created_at),
        )
    # A scan of a different path must not show up in this path's picker.
    cur.execute(
        """
        INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Other", 999, 1000, 10, 3, "2024-04-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    rows = history.list_scans_for_path("C:/Example")

    assert len(rows) == 3
    # Newest first, by created_at — lets the comparison picker default to
    # the two most recent without an extra sort step.
    assert [created_at for _id, created_at, _size, _files in rows] == [
        "2024-03-01T00:00:00", "2024-02-01T00:00:00", "2024-01-01T00:00:00",
    ]
    assert [size for _id, _created_at, size, _files in rows] == [150, 200, 100]


def test_record_and_get_audit_entry_round_trips(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    history.record_audit_entry(
        source="Duplicate Files", action="recycle", path="/Users/me/dup.bin",
        is_dir=False, size_bytes=1234, success=True,
    )
    history.record_audit_entry(
        source="Main tree", action="recycle", path="/Users/me/locked",
        is_dir=True, size_bytes=999, success=False,
        error_message="It may be in use, protected, or require admin rights.",
    )

    rows = history.get_audit_log()

    assert len(rows) == 2
    # Most recent first.
    created_at, source, action, path, is_dir, size_bytes, success, error_message = rows[0]
    assert source == "Main tree"
    assert action == "recycle"
    assert path == "/Users/me/locked"
    assert is_dir == 1
    assert size_bytes == 999
    assert success == 0
    assert "protected" in error_message

    second = rows[1]
    assert second[1] == "Duplicate Files"
    assert second[5] == 1234
    assert second[6] == 1
    assert second[7] is None


def test_get_audit_log_respects_limit(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    for i in range(5):
        history.record_audit_entry(
            source="Search & Filter", action="recycle", path=f"/tmp/f{i}.bin",
            is_dir=False, size_bytes=i, success=True,
        )

    assert len(history.get_audit_log(limit=3)) == 3
    assert len(history.get_audit_log(limit=100)) == 5
