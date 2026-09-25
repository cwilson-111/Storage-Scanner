"""Migrating a scan-history database written by the version 1 layout (one
folder_snapshots row per scan and folder, holding the full path text) to
the compact version 2 layout (storage_scanner.history_schema)."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner import history_schema

# The version 1 DDL exactly as init_history_db() created it.
V1_TABLES = (
    """
    CREATE TABLE scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_path TEXT NOT NULL,
        total_size INTEGER NOT NULL,
        drive_capacity INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        folder_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE folder_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER NOT NULL,
        folder_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(scan_id) REFERENCES scans(id)
    )
    """,
    """
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        source TEXT NOT NULL,
        action TEXT NOT NULL,
        path TEXT NOT NULL,
        is_dir INTEGER NOT NULL,
        size_bytes INTEGER NOT NULL,
        success INTEGER NOT NULL,
        error_message TEXT
    )
    """,
    "CREATE INDEX idx_scans_path ON scans(scan_path)",
    "CREATE INDEX idx_folder_scan ON folder_snapshots(scan_id)",
    "CREATE INDEX idx_folder_path ON folder_snapshots(folder_path)",
    "CREATE INDEX idx_audit_created_at ON audit_log(created_at)",
    """
    CREATE TABLE budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,
        threshold_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE known_install_locations (
        install_location TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_installed_at TEXT NOT NULL,
        currently_installed INTEGER NOT NULL
    )
    """,
)
V1_APP_METADATA = "CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"

# (scan id, scan_path, created_at, {folder: size})
SCANS = (
    (1, "c:\\data", "2026-01-01T10:00:00", {"a": 400, "b": 300, "gone": 200}),
    (2, "c:\\data", "2026-02-01T10:00:00", {"a": 500, "b": 300, "new": 250}),
    (3, "c:\\", "2026-02-02T10:00:00", {"a": 500, "other": 9000}),
    (4, "c:\\data", "2026-03-01T10:00:00", {"a": 450, "b": 350, "new": 250, "newer": 100}),
)
# The version 1 growth query, verbatim.
V1_GROWTH = """
    SELECT
        curr.folder_path,
        COALESCE(prev.size_bytes, 0) AS previous_size,
        curr.size_bytes AS current_size,
        curr.size_bytes - COALESCE(prev.size_bytes, 0) AS growth_bytes,
        curr.file_count
    FROM folder_snapshots curr
    LEFT JOIN folder_snapshots prev
        ON curr.folder_path = prev.folder_path
       AND prev.scan_id = ?
    WHERE curr.scan_id = ?
    ORDER BY growth_bytes DESC
    LIMIT ?
"""
PAIRS = ((2, 1), (4, 2), (4, 1), (3, 2))


def _folder(scan_path, name):
    return scan_path.rstrip("\\") + "\\" + name


def _make_v1_database(db_path, with_app_metadata=True, folders_per_scan=None):
    conn = sqlite3.connect(db_path)
    if with_app_metadata:
        conn.execute(V1_APP_METADATA)
        conn.execute("INSERT INTO app_metadata VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO app_metadata VALUES ('turbo_scan_enabled', '1')")
    for statement in V1_TABLES:
        conn.execute(statement)
    for scan_id, scan_path, created_at, folders in SCANS:
        folders = {**folders, **(folders_per_scan or {})}
        conn.execute(
            "INSERT INTO scans VALUES (?, ?, ?, 1000000, ?, ?, ?)",
            (scan_id, scan_path, sum(folders.values()), 10, len(folders), created_at),
        )
        conn.executemany(
            "INSERT INTO folder_snapshots (scan_id, folder_path, size_bytes, file_count, "
            "created_at) VALUES (?, ?, ?, 3, ?)",
            [(scan_id, _folder(scan_path, n), size, created_at) for n, size in folders.items()],
        )
    # A row whose scan no longer exists: nothing can read it.
    conn.execute(
        "INSERT INTO folder_snapshots (scan_id, folder_path, size_bytes, file_count, created_at) "
        "VALUES (99, 'c:\\orphan', 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO audit_log VALUES (1, '2026-01-05T00:00:00', 'Main tree', 'recycle', "
        "'c:\\data\\x', 0, 12, 1, NULL)"
    )
    conn.execute("INSERT INTO budgets VALUES (1, 'c:\\data', 5000, '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO known_install_locations VALUES ('c:\\program files\\app', 'App', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 1)"
    )
    conn.commit()
    conn.close()


def _v1_growth(db_path, current, previous, limit=50):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(V1_GROWTH, (previous, current, limit)).fetchall()
    conn.close()
    # Version 1 left equal growth in no particular order; version 2 orders
    # it by path.
    return sorted(rows, key=lambda row: (-row[3], row[0]))


def _query(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _everything(db_path):
    """Every row of every table, for comparing a database before and after."""
    tables = [
        name for (name,) in _query(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    return {table: sorted(_query(db_path, f"SELECT * FROM {table}")) for table in tables}


@pytest.fixture
def v1_db(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    return db_path


@pytest.mark.parametrize("with_app_metadata", [True, False])
def test_a_version_1_database_is_migrated_with_every_scan_and_folder_row(v1_db, with_app_metadata):
    _make_v1_database(v1_db, with_app_metadata)
    growth_before = {pair: _v1_growth(v1_db, *pair) for pair in PAIRS}
    scans_before = _query(v1_db, "SELECT * FROM scans ORDER BY id")

    history.init_history_db()

    assert history.get_app_metadata("schema_version") == str(history_schema.SCHEMA_VERSION)
    names = {name for (name,) in _query(v1_db, "SELECT name FROM sqlite_master")}
    assert not names & {"folder_snapshots_v1", "idx_folder_scan", "idx_folder_path"}
    columns = [row[1] for row in _query(v1_db, "PRAGMA table_info(folder_snapshots)")]
    assert columns == ["scan_id", "path_id", "size_bytes", "file_count"]

    assert _query(v1_db, "SELECT * FROM scans ORDER BY id") == scans_before
    for (current, previous), rows in growth_before.items():
        migrated = history.get_folder_growth(current, previous)
        assert [row[:4] + row[6:] for row in migrated] == rows
    folder_rows = sum(len(folders) for *_scan, folders in SCANS)
    assert _query(v1_db, "SELECT COUNT(*) FROM folder_snapshots") == [(folder_rows,)]
    assert "c:\\orphan" not in {path for (path,) in _query(v1_db, "SELECT path FROM folder_paths")}

    assert history.list_budgets() == [(1, "c:\\data", 5000, "2026-01-01T00:00:00")]
    assert len(history.get_audit_log()) == 1
    assert _query(v1_db, "SELECT COUNT(*) FROM known_install_locations") == [(1,)]
    if with_app_metadata:
        assert history.get_app_metadata("turbo_scan_enabled") == "1"


def test_a_migrated_database_is_left_alone_by_every_later_start(v1_db):
    _make_v1_database(v1_db)
    history.init_history_db()
    migrated = _everything(v1_db)

    history.init_history_db()

    assert _everything(v1_db) == migrated


def test_scans_saved_after_migrating_compare_with_migrated_ones(v1_db):
    _make_v1_database(v1_db)
    history.init_history_db()
    (paths_before,) = _query(v1_db, "SELECT COUNT(*) FROM folder_paths")

    scan_id = history.save_scan_snapshot(
        "c:\\data",
        900,
        1000000,
        10,
        3,
        {
            "c:\\data\\a": {"size": 460, "file_count": 3},
            "c:\\data\\brand_new": {"size": 440, "file_count": 3},
        },
    )

    assert _query(v1_db, "SELECT COUNT(*) FROM folder_paths") == [(paths_before[0] + 1,)]
    rows = history.get_folder_growth(scan_id, 4)
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("c:\\data\\brand_new", 0, 440),
        ("c:\\data\\a", 450, 460),
    ]


def test_a_failed_migration_leaves_the_version_1_database_as_it_was(v1_db, monkeypatch):
    _make_v1_database(v1_db)
    original = _everything(v1_db)

    def fail(_conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(history_schema, "_copy_v1_folder_rows", fail)
    with pytest.raises(sqlite3.OperationalError):
        history.init_history_db()

    assert _everything(v1_db) == original
    monkeypatch.undo()
    monkeypatch.setattr(history, "DB_NAME", str(v1_db))
    history.init_history_db()
    assert history.get_app_metadata("schema_version") == str(history_schema.SCHEMA_VERSION)


def test_migrating_reclaims_the_old_layouts_space(v1_db):
    many = {f"some folder with a longish name {i:05d}": 1000 + i for i in range(3000)}
    _make_v1_database(v1_db, folders_per_scan=many)
    size_before = os.path.getsize(v1_db)

    history.init_history_db()

    assert os.path.getsize(v1_db) < size_before / 2
