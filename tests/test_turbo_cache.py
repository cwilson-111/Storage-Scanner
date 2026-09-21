"""Tests for storage_scanner.turbo_cache against hand-built ParsedRecord
lists -- pure SQLite round-tripping, no ctypes/Win32/elevated access needed
(that's storage_scanner.usn_journal's concern -- see tests/test_usn_journal.py).
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import turbo_cache
from storage_scanner.mft_parser import FileNameAttr, ParsedRecord, _pack_frn

VOLUME_SERIAL = 123456789
ROOT_FRN = _pack_frn(1, 5)


def _init_db(tmp_path, monkeypatch):
    db_path = tmp_path / "turbo_scan_cache.db"
    monkeypatch.setattr(turbo_cache, "DB_NAME", db_path)
    turbo_cache.init_cache_db()
    return db_path


def _frn(record_number, sequence_number=1):
    return _pack_frn(sequence_number, record_number)


def _name(parent_frn, name, namespace=1):
    return FileNameAttr(parent_frn=parent_frn, name=name, namespace=namespace)


def _record(
    record_number, *, is_directory=False, names=(), sequence_number=1,
    logical_size=0, alloc_size=0, is_reparse_point=False,
    is_cloud_placeholder=False, mtime=0.0, atime=0.0, file_attributes=0,
):
    return ParsedRecord(
        frn=_frn(record_number, sequence_number), is_directory=is_directory,
        file_attributes=file_attributes, is_reparse_point=is_reparse_point,
        is_cloud_placeholder=is_cloud_placeholder, mtime=mtime, atime=atime,
        logical_size=logical_size, alloc_size=alloc_size, names=list(names),
    )


def test_init_cache_db_is_idempotent_and_creates_all_tables(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    turbo_cache.init_cache_db()  # second call must not raise

    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()
    assert {"cached_volumes", "cached_records"} <= tables


def test_init_cache_db_wipes_a_pre_pickle_json_format_cache(tmp_path, monkeypatch):
    # Simulates a real on-disk cache built by an older version of this
    # module, back when cached_records stored JSON text (record_json)
    # instead of pickle blobs (record_blob). A stale JSON row can't be
    # unpickled, so init_cache_db() must detect the old schema and wipe
    # both tables -- forcing one clean full rescan next time -- rather
    # than leave a cached_volumes row whose matching records table is
    # either unreadable or (worse) silently incomplete.
    db_path = _init_db(tmp_path, monkeypatch)
    turbo_cache.save_full_scan(
        VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024,
        [_record(5, is_directory=True, names=[]), _record(10, names=[_name(ROOT_FRN, "a.txt")])],
    )

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE cached_records")
    conn.execute("""
        CREATE TABLE cached_records (
            volume_serial   INTEGER NOT NULL,
            record_number   INTEGER NOT NULL,
            frn             INTEGER NOT NULL,
            record_json     TEXT NOT NULL,
            PRIMARY KEY (volume_serial, record_number)
        )
    """)
    conn.execute(
        "INSERT INTO cached_records VALUES (?, ?, ?, ?)",
        (VOLUME_SERIAL, 5, ROOT_FRN, '{"frn": 5}'),
    )
    conn.commit()
    conn.close()

    turbo_cache.init_cache_db()  # simulates restarting on the new code

    assert turbo_cache.get_cached_volume(VOLUME_SERIAL) is None
    assert turbo_cache.load_all_records(VOLUME_SERIAL) == []

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cached_records)").fetchall()}
    conn.close()
    assert columns == {"volume_serial", "record_number", "frn", "record_blob"}


def test_get_cached_volume_returns_none_for_unseen_volume(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert turbo_cache.get_cached_volume(VOLUME_SERIAL) is None


def test_save_full_scan_then_load_all_records_round_trips(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    records = [
        _record(5, is_directory=True, names=[]),
        _record(
            10, is_directory=True, names=[_name(ROOT_FRN, "Docs")],
            file_attributes=0x10, mtime=1000.5, atime=1000.5,
        ),
        _record(  # multi-name: a real hard link across two parents
            11,
            names=[_name(_frn(10), "a.txt"), _name(ROOT_FRN, "a_link.txt")],
            logical_size=100, alloc_size=4096,
        ),
        _record(  # a genuine cloud placeholder -- also a reparse point
            12, names=[_name(_frn(10), "placeholder.bin")],
            is_reparse_point=True, is_cloud_placeholder=True,
            file_attributes=0x400 | 0x1000,
        ),
    ]

    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, records)
    loaded = turbo_cache.load_all_records(VOLUME_SERIAL)

    assert sorted(loaded, key=lambda r: r.frn) == sorted(records, key=lambda r: r.frn)

    cached_volume = turbo_cache.get_cached_volume(VOLUME_SERIAL)
    assert cached_volume["volume_root"] == "C:\\"
    assert cached_volume["root_frn"] == ROOT_FRN
    assert cached_volume["record_size"] == 1024
    assert cached_volume["usn_journal_id"] is None
    assert cached_volume["next_usn"] is None


def test_load_all_records_raises_turbo_cache_corrupt_error_on_a_truncated_blob(tmp_path, monkeypatch):
    """A truncated/corrupt record_blob (interrupted write, disk error)
    must surface as TurboCacheCorruptError specifically, not an
    unqualified pickle exception -- turbo_scan.get_records_using_cache
    catches this exact type to invalidate the volume and self-heal on
    the next scan, instead of repeating the same failure forever."""
    db_path = _init_db(tmp_path, monkeypatch)
    turbo_cache.save_full_scan(
        VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, [_record(5, is_directory=True, names=[])],
    )

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE cached_records SET record_blob = ? WHERE volume_serial = ?",
        (b"not a valid pickle blob", VOLUME_SERIAL),
    )
    conn.commit()
    conn.close()

    try:
        turbo_cache.load_all_records(VOLUME_SERIAL)
    except turbo_cache.TurboCacheCorruptError:
        pass
    else:
        raise AssertionError("expected TurboCacheCorruptError for a corrupt record blob")


def test_second_save_full_scan_replaces_the_previous_record_set(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    first = [_record(5, is_directory=True, names=[]), _record(10, names=[_name(ROOT_FRN, "old.txt")])]
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, first)

    second = [_record(5, is_directory=True, names=[])]  # "old.txt" no longer exists
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, second)

    loaded = turbo_cache.load_all_records(VOLUME_SERIAL)
    assert len(loaded) == 1
    assert loaded[0].frn == _frn(5)


def test_save_journal_cursor_updates_the_volume_row(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, [_record(5, is_directory=True, names=[])])

    turbo_cache.save_journal_cursor(VOLUME_SERIAL, usn_journal_id=42, next_usn=1000)

    cached_volume = turbo_cache.get_cached_volume(VOLUME_SERIAL)
    assert cached_volume["usn_journal_id"] == 42
    assert cached_volume["next_usn"] == 1000


def test_apply_incremental_changes_upserts_in_place_not_a_second_row(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    original = _record(11, names=[_name(ROOT_FRN, "a.txt")], logical_size=100)
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, [_record(5, is_directory=True, names=[]), original])
    turbo_cache.save_journal_cursor(VOLUME_SERIAL, usn_journal_id=42, next_usn=1000)

    updated = _record(11, names=[_name(ROOT_FRN, "a.txt")], logical_size=200)  # same record_number, size changed
    turbo_cache.apply_incremental_changes(
        VOLUME_SERIAL, upserts=[updated], deletes=[], new_next_usn=1005,
    )

    loaded = {r.frn: r for r in turbo_cache.load_all_records(VOLUME_SERIAL)}
    assert len(loaded) == 2  # still just root + record 11, no duplicate row
    assert loaded[_frn(11)].logical_size == 200
    assert turbo_cache.get_cached_volume(VOLUME_SERIAL)["next_usn"] == 1005


def test_apply_incremental_changes_deletes_a_record(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    deleted_record = _record(11, names=[_name(ROOT_FRN, "gone.txt")])
    turbo_cache.save_full_scan(
        VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024,
        [_record(5, is_directory=True, names=[]), deleted_record],
    )
    turbo_cache.save_journal_cursor(VOLUME_SERIAL, usn_journal_id=42, next_usn=1000)

    turbo_cache.apply_incremental_changes(
        VOLUME_SERIAL, upserts=[], deletes=[11], new_next_usn=1005,
    )

    loaded = turbo_cache.load_all_records(VOLUME_SERIAL)
    assert len(loaded) == 1
    assert loaded[0].frn == _frn(5)


def test_invalidate_volume_cascades_to_cached_records(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    turbo_cache.save_full_scan(
        VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024,
        [_record(5, is_directory=True, names=[]), _record(10, names=[_name(ROOT_FRN, "a.txt")])],
    )

    turbo_cache.invalidate_volume(VOLUME_SERIAL)

    assert turbo_cache.get_cached_volume(VOLUME_SERIAL) is None
    assert turbo_cache.load_all_records(VOLUME_SERIAL) == []


def test_reused_record_number_is_stored_under_its_new_frn(tmp_path, monkeypatch):
    # The real-world case this covers: an MFT record slot is freed (file
    # deleted) and reused for a completely different file, bumping the
    # sequence number -- record_number stays the same, frn does not.
    _init_db(tmp_path, monkeypatch)
    original = _record(11, names=[_name(ROOT_FRN, "first.txt")], sequence_number=1)
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, [_record(5, is_directory=True, names=[]), original])

    reused = _record(11, names=[_name(ROOT_FRN, "second.txt")], sequence_number=2)
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, [_record(5, is_directory=True, names=[]), reused])

    loaded = {r.frn: r for r in turbo_cache.load_all_records(VOLUME_SERIAL)}
    assert len(loaded) == 2
    assert loaded[_frn(11, sequence_number=2)].names[0].name == "second.txt"
    assert _frn(11, sequence_number=1) not in loaded
