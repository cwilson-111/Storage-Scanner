"""Tests for storage_scanner.turbo_cache against hand-built ParsedRecord
lists -- pure SQLite round-tripping, no ctypes/Win32/elevated access needed
(that's storage_scanner.usn_journal's concern -- see tests/test_usn_journal.py).
"""

import sqlite3
import sys
from pathlib import Path

import pytest

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
    record_number,
    *,
    is_directory=False,
    names=(),
    sequence_number=1,
    logical_size=0,
    alloc_size=0,
    is_reparse_point=False,
    is_cloud_placeholder=False,
    mtime=0.0,
    atime=0.0,
    file_attributes=0,
):
    return ParsedRecord(
        frn=_frn(record_number, sequence_number),
        is_directory=is_directory,
        file_attributes=file_attributes,
        is_reparse_point=is_reparse_point,
        is_cloud_placeholder=is_cloud_placeholder,
        mtime=mtime,
        atime=atime,
        logical_size=logical_size,
        alloc_size=alloc_size,
        names=list(names),
    )


def _root():
    return _record(5, is_directory=True)


def _save(records):
    turbo_cache.save_full_scan(VOLUME_SERIAL, "C:\\", ROOT_FRN, 1024, records)


def _load(*parts):
    """{frn: record} for everything a scan of `parts` would load."""
    target, _actual = turbo_cache.find_record_by_path(VOLUME_SERIAL, ROOT_FRN, list(parts))
    records = turbo_cache.load_subtree_records(VOLUME_SERIAL, target)
    for record in records:
        record.names.sort(key=lambda n: (n.parent_frn, n.name))
    return {r.frn: r for r in records}


def _row_count(db_path, table):
    conn = sqlite3.connect(db_path)
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return count


# -- schema ------------------------------------------------------------------- #


def test_init_cache_db_is_idempotent_and_creates_all_tables(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    turbo_cache.init_cache_db()  # second call must not raise

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    conn.close()
    assert {"cached_volumes", "cached_records", "cached_names"} <= tables


@pytest.mark.parametrize("old_column", ["record_blob", "record_json"])
def test_init_cache_db_wipes_an_older_single_value_cache(tmp_path, monkeypatch, old_column):
    # A real on-disk cache from an older version: one pickle (record_blob) or
    # JSON (record_json) value per record. It must be wiped entirely --
    # volumes row included -- so the next scan does one clean full read
    # instead of trusting a volume row with no readable records behind it.
    db_path = tmp_path / "turbo_scan_cache.db"
    monkeypatch.setattr(turbo_cache, "DB_NAME", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE cached_volumes (
            volume_serial INTEGER PRIMARY KEY, volume_root TEXT NOT NULL,
            usn_journal_id INTEGER, next_usn INTEGER, root_frn INTEGER NOT NULL,
            record_size INTEGER NOT NULL, full_scan_completed_at TEXT NOT NULL,
            last_refreshed_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO cached_volumes VALUES (?, 'C:\\', 7, 100, ?, 1024, 'x', 'x')",
        (VOLUME_SERIAL, ROOT_FRN),
    )
    conn.execute(f"""
        CREATE TABLE cached_records (
            volume_serial INTEGER NOT NULL, record_number INTEGER NOT NULL,
            frn INTEGER NOT NULL, {old_column} BLOB NOT NULL,
            PRIMARY KEY (volume_serial, record_number)
        )
    """)
    conn.execute("INSERT INTO cached_records VALUES (?, 5, ?, x'00')", (VOLUME_SERIAL, ROOT_FRN))
    conn.commit()
    conn.close()

    turbo_cache.init_cache_db()  # the new code starting up

    assert turbo_cache.get_cached_volume(VOLUME_SERIAL) is None
    assert _row_count(db_path, "cached_records") == 0


# -- saving and loading ---------------------------------------------------------- #


def test_get_cached_volume_returns_none_for_unseen_volume(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert turbo_cache.get_cached_volume(VOLUME_SERIAL) is None


def test_save_full_scan_then_load_round_trips_every_field(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    records = [
        _root(),
        _record(
            10,
            is_directory=True,
            names=[_name(ROOT_FRN, "Docs")],
            file_attributes=0x10,
            mtime=1000.5,
            atime=1000.25,
        ),
        _record(  # a real hard link across two parents, both inside the scan
            11,
            names=[_name(ROOT_FRN, "a_link.txt"), _name(_frn(10), "a.txt")],
            logical_size=100,
            alloc_size=4096,
        ),
        _record(  # a genuine cloud placeholder -- also a reparse point
            12,
            names=[_name(_frn(10), "placeholder.bin")],
            is_reparse_point=True,
            is_cloud_placeholder=True,
            file_attributes=0x400 | 0x1000,
        ),
    ]

    _save(records)

    assert _load() == {r.frn: r for r in records}
    cached_volume = turbo_cache.get_cached_volume(VOLUME_SERIAL)
    assert cached_volume["volume_root"] == "C:\\"
    assert cached_volume["root_frn"] == ROOT_FRN
    assert cached_volume["record_size"] == 1024
    assert cached_volume["usn_journal_id"] is None
    assert cached_volume["next_usn"] is None


def test_a_folder_rescan_loads_only_that_folder(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    _save(
        [
            _root(),
            _record(10, is_directory=True, names=[_name(ROOT_FRN, "Docs")]),
            _record(20, is_directory=True, names=[_name(ROOT_FRN, "Other")]),
            _record(11, names=[_name(_frn(10), "a.txt"), _name(_frn(20), "a_link.txt")]),
            _record(21, names=[_name(_frn(20), "b.txt")]),
        ]
    )

    loaded = _load("Docs")

    assert set(loaded) == {_frn(10), _frn(11)}
    # The hard link living in Other isn't part of this scan, so it isn't
    # handed to build_tree as an unreachable orphan.
    assert loaded[_frn(11)].names == [_name(_frn(10), "a.txt")]


def test_a_reparse_point_is_followed_only_when_it_is_the_folder_asked_for(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    _save(
        [
            _root(),
            _record(10, is_directory=True, is_reparse_point=True, names=[_name(ROOT_FRN, "Link")]),
            _record(11, names=[_name(_frn(10), "inside.txt")]),
        ]
    )

    assert _frn(11) not in _load()  # a junction met during a scan stays a leaf
    assert _frn(11) in _load("Link")  # but scanning the junction itself follows it


def test_find_record_by_path_matches_case_insensitively_and_returns_disk_spelling(
    tmp_path, monkeypatch
):
    _init_db(tmp_path, monkeypatch)
    _save(
        [
            _root(),
            _record(10, is_directory=True, names=[_name(ROOT_FRN, "Users")]),
            _record(11, is_directory=True, names=[_name(_frn(10), "Jérôme")]),
        ]
    )

    record, actual = turbo_cache.find_record_by_path(VOLUME_SERIAL, ROOT_FRN, ["users", "JÉRÔME"])

    assert record.frn == _frn(11)
    assert actual == ["Users", "Jérôme"]


def test_find_record_by_path_refuses_missing_names_and_paths_through_files_or_links(
    tmp_path, monkeypatch
):
    _init_db(tmp_path, monkeypatch)
    _save(
        [
            _root(),
            _record(10, names=[_name(ROOT_FRN, "file.txt")]),
            _record(11, is_directory=True, is_reparse_point=True, names=[_name(ROOT_FRN, "Link")]),
            _record(12, names=[_name(_frn(11), "inside.txt")]),
        ]
    )

    def find(*parts):
        return turbo_cache.find_record_by_path(VOLUME_SERIAL, ROOT_FRN, list(parts))

    assert find("missing") is None
    assert find("file.txt", "anything") is None
    assert find("Link", "inside.txt") is None  # a scan never walks into a link
    assert find("Link")[0].frn == _frn(11)


def test_a_damaged_database_raises_turbo_cache_corrupt_error(tmp_path, monkeypatch):
    """A damaged cache file must surface as TurboCacheCorruptError, not a
    bare sqlite3 error -- turbo_read catches exactly that type to
    invalidate the volume and self-heal with a full read next time."""
    db_path = _init_db(tmp_path, monkeypatch)
    _save([_root()] + [_record(n, names=[_name(ROOT_FRN, f"f{n}.txt")]) for n in range(10, 400)])
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(db_path, "r+b") as f:  # keep page 1 (the header) intact
        f.seek(4096)
        f.write(b"\xde\xad" * 8192)

    with pytest.raises(turbo_cache.TurboCacheCorruptError):
        turbo_cache.find_record_by_path(VOLUME_SERIAL, ROOT_FRN, ["f10.txt"])


def test_second_save_full_scan_replaces_the_previous_record_set(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    _save([_root(), _record(10, names=[_name(ROOT_FRN, "old.txt")])])

    _save([_root()])  # "old.txt" no longer exists

    assert set(_load()) == {ROOT_FRN}


def test_save_journal_cursor_updates_the_volume_row(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    _save([_root()])

    turbo_cache.save_journal_cursor(VOLUME_SERIAL, usn_journal_id=42, next_usn=1000)

    cached_volume = turbo_cache.get_cached_volume(VOLUME_SERIAL)
    assert cached_volume["usn_journal_id"] == 42
    assert cached_volume["next_usn"] == 1000


# -- incremental changes ---------------------------------------------------------- #


def test_apply_incremental_changes_updates_in_place_and_replaces_renamed_names(
    tmp_path, monkeypatch
):
    db_path = _init_db(tmp_path, monkeypatch)
    _save([_root(), _record(11, names=[_name(ROOT_FRN, "a.txt")], logical_size=100)])
    turbo_cache.save_journal_cursor(VOLUME_SERIAL, usn_journal_id=42, next_usn=1000)

    renamed = _record(11, names=[_name(ROOT_FRN, "b.txt")], logical_size=200)
    turbo_cache.apply_incremental_changes(
        VOLUME_SERIAL, upserts=[renamed], deletes=[], new_next_usn=1005
    )

    loaded = _load()
    assert set(loaded) == {ROOT_FRN, _frn(11)}  # no duplicate row
    assert loaded[_frn(11)].logical_size == 200
    assert loaded[_frn(11)].names == [_name(ROOT_FRN, "b.txt")]  # old name gone
    assert _row_count(db_path, "cached_names") == 1
    assert turbo_cache.get_cached_volume(VOLUME_SERIAL)["next_usn"] == 1005


def test_apply_incremental_changes_deletes_a_record_and_its_names(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _save([_root(), _record(11, names=[_name(ROOT_FRN, "gone.txt")])])

    turbo_cache.apply_incremental_changes(VOLUME_SERIAL, upserts=[], deletes=[11], new_next_usn=1)

    assert set(_load()) == {ROOT_FRN}
    assert _row_count(db_path, "cached_names") == 0


def test_invalidate_volume_cascades_to_records_and_names(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _save([_root(), _record(10, names=[_name(ROOT_FRN, "a.txt")])])

    turbo_cache.invalidate_volume(VOLUME_SERIAL)

    assert turbo_cache.get_cached_volume(VOLUME_SERIAL) is None
    assert _row_count(db_path, "cached_records") == 0
    assert _row_count(db_path, "cached_names") == 0


def test_reused_record_number_is_stored_under_its_new_frn(tmp_path, monkeypatch):
    # The real-world case this covers: an MFT record slot is freed (file
    # deleted) and reused for a completely different file, bumping the
    # sequence number -- record_number stays the same, frn does not.
    _init_db(tmp_path, monkeypatch)
    _save([_root(), _record(11, names=[_name(ROOT_FRN, "first.txt")], sequence_number=1)])

    _save([_root(), _record(11, names=[_name(ROOT_FRN, "second.txt")], sequence_number=2)])

    loaded = _load()
    assert set(loaded) == {ROOT_FRN, _frn(11, sequence_number=2)}
    assert loaded[_frn(11, sequence_number=2)].names[0].name == "second.txt"
