"""A history database the app can't use must not stop it starting (P1-6):
a damaged file is moved aside and a new history started, a database from a
newer version is read but never written, and an older one is copied before
it's migrated."""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import (
    app,
    history_db,
    history_queries,
    history_records,
    history_schema,
    history_store,
)

JUNK = b"this is not a database, just some bytes that happen to be here\n" * 64


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history_db, "DB_NAME", str(path))
    return path


def _everything(db_path):
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {table: sorted(conn.execute(f"SELECT * FROM {table}")) for table in tables}
    finally:
        conn.close()


def _set_schema_version(db_path, version):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE app_metadata SET value = ? WHERE key = 'schema_version'", (str(version),))
    conn.commit()
    conn.close()


def _junk_file(db_path):
    db_path.write_bytes(JUNK)


def _database_with_a_wrecked_schema_page(db_path):
    history_db.init_history_db()
    history_store.save_scan_snapshot("c:\\data", 1, 1, 1, 1, {})
    data = bytearray(db_path.read_bytes())
    data[100:4096] = b"\xff" * (4096 - 100)  # page 1's b-tree: the table list
    db_path.write_bytes(bytes(data))


@pytest.mark.parametrize(
    "damage, error",
    [
        (_junk_file, "file is not a database"),
        (_database_with_a_wrecked_schema_page, "database disk image is malformed"),
    ],
)
def test_a_damaged_file_is_moved_aside_and_the_app_starts_with_a_new_history(
    db_path, damage, error
):
    damage(db_path)
    damaged_bytes = db_path.read_bytes()

    warning = app._open_history()

    (moved,) = db_path.parent.glob("storage_history.damaged-*.db")
    assert moved.read_bytes() == damaged_bytes
    assert str(moved) in warning
    assert error in warning
    # The new history works, and starts empty.
    assert history_db.get_app_metadata("schema_version") == str(history_schema.SCHEMA_VERSION)
    assert history_queries.get_most_recent_scan_path() is None
    scan_id = history_store.save_scan_snapshot("c:\\data", 1, 1, 1, 1, {})
    assert history_queries.get_latest_scan_id("c:\\data") == scan_id
    # The next start has nothing to say.
    assert app._open_history() is None
    assert len(list(db_path.parent.glob("storage_history.damaged-*.db"))) == 1


def test_a_stale_write_ahead_log_is_not_read_into_the_new_history(db_path):
    """A -wal left beside a damaged file must not become the new database's
    own. (SQLite drops an unusable -wal itself; move_aside takes any that
    are left along with the file.)"""
    _junk_file(db_path)
    stale = b"stale write-ahead log" * 100
    Path(f"{db_path}-wal").write_bytes(stale)

    app._open_history()

    wal = Path(f"{db_path}-wal")
    assert not wal.exists() or stale not in wal.read_bytes()
    assert history_queries.get_most_recent_scan_path() is None
    scan_id = history_store.save_scan_snapshot("c:\\new", 1, 1, 1, 1, {})
    assert history_queries.get_latest_scan_id("c:\\new") == scan_id


def test_a_locked_database_is_not_mistaken_for_a_damaged_one(db_path, monkeypatch):
    history_db.init_history_db()
    history_store.save_scan_snapshot("c:\\data", 1, 1, 1, 1, {})
    monkeypatch.setattr(history_db, "BUSY_TIMEOUT_SECONDS", 0.1)
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            history_db.open_history_db()
    finally:
        holder.rollback()
        holder.close()

    assert list(db_path.parent.glob("storage_history.damaged-*")) == []
    assert history_queries.get_latest_scan_id("c:\\data") is not None


def test_a_newer_database_is_read_but_never_written(db_path):
    history_db.init_history_db()
    scan_id = history_store.save_scan_snapshot(
        "c:\\data", 100, 1000, 1, 1, {"c:\\data": {"size": 100}}
    )
    history_records.set_budget("c:\\data", 50)
    newer = history_schema.SCHEMA_VERSION + 1
    _set_schema_version(db_path, newer)
    before = _everything(db_path)

    with pytest.raises(history_schema.NewerDatabaseError) as raised:
        history_db.init_history_db()
    warning = app._open_history()

    message = str(raised.value)
    assert f"database version {newer}" in message
    assert f"up to {history_schema.SCHEMA_VERSION}" in message
    assert warning == message
    writes = [
        lambda: history_store.save_scan_snapshot("c:\\data", 200, 1000, 2, 1, {}),
        lambda: history_store.delete_scan(scan_id),
        lambda: history_records.set_budget("c:\\other", 10),
        lambda: history_records.delete_budget(1),
        lambda: history_records.record_audit_entry(
            "Main tree", "recycle", "c:\\x", False, 1, "recycled"
        ),
        lambda: history_records.record_install_locations_snapshot([("App", "c:\\app")]),
    ]
    for write in writes:
        with pytest.raises(history_schema.NewerDatabaseError):
            write()
    assert _everything(db_path) == before
    assert list(db_path.parent.glob("storage_history.*-*.db")) == []
    # Reading still works.
    assert history_queries.list_scans_for_path("c:\\data")[0][0] == scan_id


def _make_version_3_database(db_path):
    """A database as version 3 of the schema left it: no drive-space
    columns on scans."""
    history_db.init_history_db()
    history_store.save_scan_snapshot("c:\\data", 100, 1000, 1, 1, {"c:\\data": {"size": 100}})
    conn = sqlite3.connect(db_path)
    for column in ("allocated_size", "drive_used", "drive_free"):
        conn.execute(f"ALTER TABLE scans DROP COLUMN {column}")
    conn.execute("UPDATE app_metadata SET value = '3' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


def test_a_version_3_database_is_copied_before_it_is_migrated(db_path):
    _make_version_3_database(db_path)
    before = _everything(db_path)

    history_db.init_history_db()

    (backup,) = db_path.parent.glob("storage_history.v3-backup-*.db")
    assert _everything(backup) == before
    assert history_db.get_app_metadata("schema_version") == str(history_schema.SCHEMA_VERSION)
    assert history_queries.get_forecast_history("c:\\data") == [(before["scans"][0][6], 100, None)]
    # Once current, starting again copies nothing.
    history_db.init_history_db()
    assert len(list(db_path.parent.glob("storage_history.v3-backup-*.db"))) == 1


def test_a_new_database_is_not_backed_up(db_path):
    history_db.init_history_db()

    assert list(db_path.parent.glob("storage_history.*-*.db")) == []
