"""Tests for storage_scanner.turbo_read's decisions: which way it reads a
volume (the cache refreshed from the USN journal, or a full MFT read), and
how it reacts to each failure. turbo_cache/usn_journal are mocked here (each
has its own suite, test_turbo_cache.py/test_usn_journal.py) and so is the
final tree building; test_turbo_scan_integration.py drives the real, unmocked
wiring between all of them on a faked NTFS volume.
"""

import dataclasses
import queue
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import turbo_read, usn_journal
from storage_scanner.models import Node
from storage_scanner.turbo_read import MftRead, find_subtree_node

# Captured before the autouse fixture below replaces it with a marker.
_REAL_SUBTREE_FROM_CACHE = turbo_read._subtree_from_cache

CACHED_VOLUME = {
    "record_size": 1024,
    "next_usn": 500,
    "usn_journal_id": 7,
    "volume_serial": 1,
    "root_frn": (1 << 48) | 5,
}
JOURNAL = usn_journal.JournalState(
    journal_id=7, first_usn=0, next_usn=500, lowest_valid_usn=0, max_usn=9999
)


@pytest.fixture(autouse=True)
def _no_real_cache_or_tree(monkeypatch):
    """Keeps every test off the real %LOCALAPPDATA% cache DB, and replaces
    the two tree builders with markers saying which path produced the
    result: ("full", record count) or "cached"."""
    monkeypatch.setattr(turbo_read.turbo_cache, "init_cache_db", lambda: None)
    monkeypatch.setattr(
        turbo_read, "_subtree_from_records", lambda records, *_a: ("full", len(records))
    )
    monkeypatch.setattr(turbo_read, "_subtree_from_cache", lambda *_a: "cached")


class _FakeRecordSource:
    def __init__(self, volume_serial=1, record_size=1024, record_count=3, raw_handle=999):
        self.volume_serial = volume_serial
        self.record_size = record_size
        self.record_count = record_count
        self.raw_handle = raw_handle


class _FakeParsedRecord:
    """Just enough of mft_parser.ParsedRecord for the decision logic
    (_full_scan_and_cache reads .frn to find the root record)."""

    def __init__(self, record_number):
        self.frn = record_number  # well under the record-number mask


def _scan(source, progress_q=None, cancel_event=None):
    return turbo_read.scan_subtree_using_cache(
        source, "C:\\", "C:\\Data", progress_q, cancel_event or threading.Event()
    )


def _full_scan_parses_everything(monkeypatch):
    monkeypatch.setattr(
        turbo_read.mft_parser, "parse_base_record", lambda n, s: _FakeParsedRecord(n)
    )
    monkeypatch.setattr(turbo_read.turbo_cache, "save_full_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_read.usn_journal,
        "ensure_journal",
        lambda handle: (_ for _ in ()).throw(usn_journal.UsnJournalError("no journal")),
    )


def _valid_cache(monkeypatch, dirty=(), new_next_usn=500):
    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda s: CACHED_VOLUME)
    monkeypatch.setattr(turbo_read.usn_journal, "query_journal", lambda handle: JOURNAL)
    monkeypatch.setattr(
        turbo_read.usn_journal,
        "read_journal_changes",
        lambda handle, journal_id, start_usn, lowest_valid_usn=None: (list(dirty), new_next_usn),
    )


# -- find_subtree_node ------------------------------------------------------- #


def _make_tree():
    root = Node("C:\\Data", "Data")
    docs = Node("C:\\Data\\Docs", "Docs")
    docs.add_file("Photo.JPG")
    root.dirs.append(docs)
    return root


def test_find_subtree_node_returns_root_for_the_root_path():
    root = _make_tree()
    assert find_subtree_node(root, "C:\\Data") is root


def test_find_subtree_node_walks_down_to_a_nested_file_case_insensitively():
    root = _make_tree()
    node = find_subtree_node(root, "C:\\Data\\Docs\\photo.jpg")
    assert node.name == "Photo.JPG"


def test_find_subtree_node_raises_for_a_missing_component():
    root = _make_tree()
    with pytest.raises(RuntimeError):
        find_subtree_node(root, "C:\\Data\\DoesNotExist")


# -- full read vs. cache ------------------------------------------------------ #


def test_no_cache_yet_does_a_full_read(monkeypatch):
    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda serial: None)
    _full_scan_parses_everything(monkeypatch)

    node, mft_read = _scan(_FakeRecordSource(record_count=3))

    assert node == ("full", 3)  # one record per record_number 0, 1, 2
    assert mft_read == MftRead(incremental=False, full_read_reason="first scan of this drive")


def test_a_valid_cache_is_refreshed_and_read_without_reparsing_the_volume(monkeypatch):
    _valid_cache(monkeypatch)
    applied = []
    monkeypatch.setattr(
        turbo_read.turbo_cache, "apply_incremental_changes", lambda *a: applied.append(a)
    )
    monkeypatch.setattr(
        turbo_read.mft_parser,
        "parse_base_record",
        lambda n, s: pytest.fail("a full read should not run on the incremental path"),
    )

    node, mft_read = _scan(_FakeRecordSource())

    assert node == "cached"
    assert mft_read == MftRead(incremental=True)
    assert applied == [(1, [], [], 500)]  # the cursor still advances with nothing to apply


def test_cache_with_no_journal_cursor_does_a_full_read(monkeypatch):
    monkeypatch.setattr(
        turbo_read.turbo_cache,
        "get_cached_volume",
        lambda serial: {"record_size": 1024, "next_usn": None},
    )
    _full_scan_parses_everything(monkeypatch)

    node, mft_read = _scan(_FakeRecordSource(record_count=2))

    assert node == ("full", 2)
    assert mft_read.full_read_reason == "no journal position cached"


def test_a_cache_for_a_different_record_size_is_not_used(monkeypatch):
    monkeypatch.setattr(
        turbo_read.turbo_cache,
        "get_cached_volume",
        lambda serial: {**CACHED_VOLUME, "record_size": 4096},
    )
    _full_scan_parses_everything(monkeypatch)

    _node, mft_read = _scan(_FakeRecordSource())

    assert mft_read.full_read_reason == "drive layout changed"


@pytest.mark.parametrize(
    "journal, reason",
    [
        (dataclasses.replace(JOURNAL, journal_id=999), "USN journal was recreated"),
        (
            dataclasses.replace(JOURNAL, first_usn=150, lowest_valid_usn=600),
            "USN journal wrapped since last scan",
        ),
    ],
)
def test_a_journal_that_cant_cover_the_gap_invalidates_and_reads_in_full(
    monkeypatch, journal, reason
):
    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda s: CACHED_VOLUME)
    monkeypatch.setattr(turbo_read.usn_journal, "query_journal", lambda handle: journal)
    invalidated = []
    monkeypatch.setattr(
        turbo_read.turbo_cache, "invalidate_volume", lambda serial: invalidated.append(serial)
    )
    _full_scan_parses_everything(monkeypatch)

    node, mft_read = _scan(_FakeRecordSource(record_count=1))

    assert node == ("full", 1)
    assert invalidated == [1]
    assert mft_read.full_read_reason == reason


def test_a_damaged_cache_invalidates_and_reads_in_full(monkeypatch):
    """A damaged cache must be treated like a stale journal: invalidate
    this volume and read in full, so the NEXT scan rebuilds cleanly instead
    of hitting the same damage (and Compatible fallback) forever."""
    _valid_cache(monkeypatch)
    monkeypatch.setattr(turbo_read.turbo_cache, "apply_incremental_changes", lambda *a: None)

    def damaged(*_a):
        raise turbo_read.turbo_cache.TurboCacheCorruptError("malformed")

    monkeypatch.setattr(turbo_read, "_subtree_from_cache", damaged)
    invalidated = []
    monkeypatch.setattr(
        turbo_read.turbo_cache, "invalidate_volume", lambda serial: invalidated.append(serial)
    )
    _full_scan_parses_everything(monkeypatch)

    node, mft_read = _scan(_FakeRecordSource(record_count=1))

    assert node == ("full", 1)
    assert invalidated == [1]
    assert mft_read.full_read_reason == "cache was corrupt"


def test_a_folder_missing_from_the_cache_is_an_error_not_a_full_read(monkeypatch):
    # A just-refreshed cache that lacks the folder means the path isn't on
    # the volume; the caller falls back to the Compatible engine.
    _valid_cache(monkeypatch)
    monkeypatch.setattr(turbo_read.turbo_cache, "apply_incremental_changes", lambda *a: None)
    monkeypatch.setattr(turbo_read.turbo_cache, "find_record_by_path", lambda *a: None)
    monkeypatch.setattr(turbo_read, "_subtree_from_cache", _REAL_SUBTREE_FROM_CACHE)
    monkeypatch.setattr(
        turbo_read,
        "_full_scan_and_cache",
        lambda *a: pytest.fail("a missing folder must not trigger a full read"),
    )

    with pytest.raises(RuntimeError, match="could not locate"):
        _scan(_FakeRecordSource())


# -- incremental refresh details ---------------------------------------------- #


def _phases(progress_q):
    phases = []
    while not progress_q.empty():
        kind, payload = progress_q.get_nowait()
        assert kind == "phase"
        phases.append(payload)
    return phases


def test_a_full_read_counts_records_read_against_the_whole_mft(monkeypatch):
    # record_count is known before the first record is read, so the bar
    # can fill for real instead of counting up to an unknown total.
    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda serial: None)
    _full_scan_parses_everything(monkeypatch)
    progress_q = queue.Queue()

    _scan(_FakeRecordSource(record_count=5000), progress_q=progress_q)

    phases = _phases(progress_q)
    reading = [p for p in phases if p.label == turbo_read.PHASE_READING_MFT]
    assert {p.total for p in reading} == {5000}
    assert reading[0].done == 0
    assert reading[-1].done == 5000
    assert [p.done for p in reading] == sorted(p.done for p in reading)
    # The slow steps after the read are named too, in the order they run.
    after = [p.label for p in phases[phases.index(reading[-1]) + 1 :]]
    assert after == [turbo_read.PHASE_SAVING_CACHE, turbo_read.PHASE_BUILDING_TREE]


def test_incremental_refresh_counts_the_journal_changes_it_applies(monkeypatch):
    # The core fix for "elevated Turbo Scan shows no progress at all" when
    # the scan turns out to be a cached incremental refresh, found via a
    # real user report.
    _valid_cache(
        monkeypatch,
        dirty=[usn_journal.DirtyRecord(record_number=n, reason=0x1) for n in range(250)],
        new_next_usn=999,
    )
    monkeypatch.setattr(
        turbo_read.mft_parser, "parse_base_record", lambda n, s: _FakeParsedRecord(n)
    )
    monkeypatch.setattr(turbo_read.turbo_cache, "apply_incremental_changes", lambda *a: None)

    progress_q = queue.Queue()
    node, _mft_read = _scan(_FakeRecordSource(), progress_q=progress_q)

    assert node == "cached"
    phases = _phases(progress_q)
    applying = [p for p in phases if p.label == turbo_read.PHASE_APPLYING_CHANGES]
    assert {p.total for p in applying} == {250}
    assert applying[-1].done == 250
    labels = [p.label for p in phases]
    assert labels[0] == turbo_read.PHASE_READING_JOURNAL
    assert labels[-1] == turbo_read.PHASE_LOADING_CACHE


def test_incremental_refresh_with_no_changes_goes_straight_to_loading(monkeypatch):
    _valid_cache(monkeypatch)
    monkeypatch.setattr(turbo_read.turbo_cache, "apply_incremental_changes", lambda *a: None)

    progress_q = queue.Queue()
    _scan(_FakeRecordSource(), progress_q=progress_q)

    labels = [p.label for p in _phases(progress_q)]
    assert labels == [turbo_read.PHASE_READING_JOURNAL, turbo_read.PHASE_LOADING_CACHE]


def test_dirty_record_that_no_longer_parses_is_deleted_not_upserted(monkeypatch):
    _valid_cache(
        monkeypatch,
        dirty=[usn_journal.DirtyRecord(record_number=42, reason=0x200)],
        new_next_usn=510,
    )
    # record 42 no longer parses -- it was deleted since the cache was built
    monkeypatch.setattr(turbo_read.mft_parser, "parse_base_record", lambda n, s: None)
    applied = {}

    def fake_apply(volume_serial, upserts, deletes, new_next_usn):
        applied.update(upserts=upserts, deletes=deletes, new_next_usn=new_next_usn)

    monkeypatch.setattr(turbo_read.turbo_cache, "apply_incremental_changes", fake_apply)

    _scan(_FakeRecordSource())

    assert applied == {"upserts": [], "deletes": [42], "new_next_usn": 510}


# -- cancellation and cache-write failure ------------------------------------- #


def test_cancelling_mid_full_read_raises_instead_of_returning_a_partial_tree(monkeypatch):
    """Turbo Scan never hands back a partial tree on abort: the full read
    must raise on cancellation, so the caller's any-failure fallback
    handles it the same as any other Turbo failure."""
    cancel_event = threading.Event()
    calls = []

    def fake_parse(record_number, record_source):
        calls.append(record_number)
        if record_number == 2:
            cancel_event.set()  # user hits Cancel partway through the volume
        return _FakeParsedRecord(record_number)

    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda serial: None)
    monkeypatch.setattr(turbo_read.mft_parser, "parse_base_record", fake_parse)
    monkeypatch.setattr(
        turbo_read.turbo_cache,
        "save_full_scan",
        lambda *a, **k: pytest.fail("a cancelled scan must never be cached"),
    )

    with pytest.raises(RuntimeError):
        _scan(_FakeRecordSource(record_count=5), cancel_event=cancel_event)

    # Checked at the top of each iteration, so the record whose parse set
    # cancel_event (2) is the last one processed.
    assert calls == [0, 1, 2]


def test_cancelling_mid_incremental_refresh_raises_instead_of_reading_in_full(monkeypatch):
    """_try_incremental_scan's None return means "no usable cursor, read in
    full"; a cancelled scan must abort instead of starting that full read."""
    cancel_event = threading.Event()
    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda s: CACHED_VOLUME)
    monkeypatch.setattr(turbo_read.usn_journal, "query_journal", lambda handle: JOURNAL)

    def fake_read_journal_changes(handle, journal_id, next_usn, lowest_valid_usn=None):
        cancel_event.set()  # user hits Cancel while applying the change set
        return [usn_journal.DirtyRecord(record_number=7, reason=0x1)], 501

    monkeypatch.setattr(turbo_read.usn_journal, "read_journal_changes", fake_read_journal_changes)
    monkeypatch.setattr(
        turbo_read.turbo_cache,
        "apply_incremental_changes",
        lambda *a: pytest.fail("a cancelled refresh must never write to the cache"),
    )
    monkeypatch.setattr(
        turbo_read,
        "_full_scan_and_cache",
        lambda *a: pytest.fail("cancellation must raise, not fall through to a full read"),
    )

    with pytest.raises(RuntimeError):
        _scan(_FakeRecordSource(record_count=1), cancel_event=cancel_event)


def test_cache_write_failure_after_a_full_read_does_not_lose_the_scan_result(monkeypatch):
    # record_count=6 so record #5 (the root) exists -- otherwise the full
    # read never even attempts to cache, and the failure path isn't hit.
    monkeypatch.setattr(turbo_read.turbo_cache, "get_cached_volume", lambda serial: None)
    monkeypatch.setattr(
        turbo_read.mft_parser, "parse_base_record", lambda n, s: _FakeParsedRecord(n)
    )

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(turbo_read.turbo_cache, "save_full_scan", locked)

    node, _mft_read = _scan(_FakeRecordSource(record_count=6))

    assert node == ("full", 6)  # caching failed silently; the scan's own result is untouched
