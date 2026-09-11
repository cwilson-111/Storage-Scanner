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
