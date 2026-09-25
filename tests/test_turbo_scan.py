import queue
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import turbo_scan, usn_journal
from storage_scanner.models import Node
from storage_scanner.turbo_scan import (
    ScanReport,
    choose_engine,
    find_subtree_node,
    scan_indicators,
)

# -- scan_indicators: what the scan-details strip shows ---------------------- #


def test_indicators_turbo_scan_complete():
    report = ScanReport(engine=turbo_scan.ENGINE_TURBO, elapsed_seconds=2.0, file_count=10_000)
    fields, complete = scan_indicators(report, 0)
    assert complete
    assert dict(fields) == {
        "Engine": "Turbo Scan (NTFS MFT)",
        "Elapsed": "2.0s",
        "Throughput": "5,000 files/s",
        "Unreadable paths": "0",
        "Result": "Complete",
    }


def test_indicators_fallback_is_named_and_unreadable_paths_mark_incomplete():
    report = ScanReport(
        engine=turbo_scan.ENGINE_COMPATIBLE,
        elapsed_seconds=1.0,
        file_count=50,
        fallback_reason="elevation was declined",
    )
    fields, complete = scan_indicators(report, 3)
    assert not complete
    assert dict(fields)["Engine"] == "Compatible (Turbo Scan fell back)"
    assert dict(fields)["Unreadable paths"] == "3"
    assert dict(fields)["Result"] == "Incomplete (some paths unreadable)"


def test_indicators_zero_elapsed_does_not_divide_by_zero():
    report = ScanReport(engine=turbo_scan.ENGINE_COMPATIBLE, elapsed_seconds=0.0, file_count=1)
    fields, _ = scan_indicators(report, 0)
    assert dict(fields)["Throughput"] == "—"


def test_indicators_without_report_from_elevated_helper():
    fields, complete = scan_indicators(None, 0)
    assert complete
    assert dict(fields)["Engine"] == "Compatible (elevated helper)"
    assert dict(fields)["Elapsed"] == "—"
    assert dict(fields)["Throughput"] == "—"


# -- choose_engine: the full decision matrix -------------------------------- #


def test_turbo_when_windows_enabled_and_ntfs_fixed(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: True)
    assert choose_engine("C:\\Data", turbo_enabled=True) == turbo_scan.ENGINE_TURBO


def test_compatible_when_not_windows(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", False)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: True)
    assert choose_engine("C:\\Data", turbo_enabled=True) == turbo_scan.ENGINE_COMPATIBLE


def test_compatible_when_turbo_disabled(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: True)
    assert choose_engine("C:\\Data", turbo_enabled=False) == turbo_scan.ENGINE_COMPATIBLE


def test_compatible_when_drive_is_not_ntfs_fixed(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: False)
    assert choose_engine("E:\\", turbo_enabled=True) == turbo_scan.ENGINE_COMPATIBLE


# -- find_subtree_node ------------------------------------------------------- #


def _make_tree():
    root = Node("C:\\Data", "Data", True)
    docs = Node("C:\\Data\\Docs", "Docs", True)
    photo = Node("C:\\Data\\Docs\\Photo.JPG", "Photo.JPG", False)
    docs.children.append(photo)
    root.children.append(docs)
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


# -- scan_with_best_engine ---------------------------------------------------- #


def _progress_and_cancel():
    return queue.Queue(), threading.Event()


def test_uses_compatible_engine_directly_when_turbo_not_applicable(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    fake_node.file_count = 7
    calls = []

    def fake_scan(*args, **kwargs):
        calls.append(1)
        return fake_node

    monkeypatch.setattr(turbo_scan.scanner, "scan", fake_scan)
    monkeypatch.setattr(
        turbo_scan, "_attempt_turbo_scan", lambda *a, **k: pytest.fail("should not be called")
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data",
        progress_q,
        cancel_event,
        turbo_enabled=False,
    )

    assert node is fake_node
    assert report == ScanReport(
        engine=turbo_scan.ENGINE_COMPATIBLE,
        elapsed_seconds=report.elapsed_seconds,
        file_count=7,
        fallback_reason=None,
    )
    assert len(calls) == 1


def test_successful_turbo_scan_reports_turbo_engine_and_skips_compatible(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    fake_node.file_count = 123
    monkeypatch.setattr(turbo_scan, "choose_engine", lambda *a, **k: turbo_scan.ENGINE_TURBO)
    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", lambda *a, **k: fake_node)
    monkeypatch.setattr(
        turbo_scan.scanner,
        "scan",
        lambda *a, **k: pytest.fail("Compatible engine should not run"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert node is fake_node
    assert report.engine == turbo_scan.ENGINE_TURBO
    assert report.file_count == 123
    assert report.fallback_reason is None


def test_turbo_failure_falls_back_to_compatible_with_a_reason(monkeypatch):
    fallback_node = Node("C:\\Data", "Data", True)
    fallback_node.file_count = 9

    monkeypatch.setattr(turbo_scan, "choose_engine", lambda *a, **k: turbo_scan.ENGINE_TURBO)

    def boom(*a, **k):
        raise RuntimeError("elevation was declined")

    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", boom)
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: fallback_node)

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert node is fallback_node
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE
    assert report.fallback_reason == "elevation was declined"


def test_turbo_disabled_setting_read_from_app_metadata_when_not_passed(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    fake_node.file_count = 1
    monkeypatch.setattr(turbo_scan, "get_app_metadata", lambda key, default: "0")
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: fake_node)
    monkeypatch.setattr(
        turbo_scan,
        "_attempt_turbo_scan",
        lambda *a, **k: pytest.fail("Turbo Scan should be disabled by app_metadata"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine("C:\\Data", progress_q, cancel_event)

    assert node is fake_node
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE


def test_already_elevated_uses_in_process_path(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)
    called = {}

    def fake_in_process(path, progress_q, cancel_event):
        called["in_process"] = path
        return fake_node

    monkeypatch.setattr(turbo_scan, "_run_turbo_in_process", fake_in_process)
    monkeypatch.setattr(
        turbo_scan,
        "_run_turbo_via_elevated_helper",
        lambda *a, **k: pytest.fail("should not spawn an elevated helper when already elevated"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    result = turbo_scan._attempt_turbo_scan("C:\\Data", progress_q, cancel_event)

    assert result is fake_node
    assert called["in_process"] == "C:\\Data"


def test_not_elevated_uses_elevated_helper_path(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", False)
    called = {}
    monkeypatch.setattr(
        turbo_scan,
        "_run_turbo_in_process",
        lambda *a, **k: pytest.fail("should not scan in-process when not elevated"),
    )

    def fake_via_helper(path, progress_q, cancel_event):
        called["via_helper"] = path
        return fake_node

    monkeypatch.setattr(turbo_scan, "_run_turbo_via_elevated_helper", fake_via_helper)

    progress_q, cancel_event = _progress_and_cancel()
    result = turbo_scan._attempt_turbo_scan("C:\\Data", progress_q, cancel_event)

    assert result is fake_node
    assert called["via_helper"] == "C:\\Data"


# -- get_records_using_cache: the cache/incremental-refresh decision logic --
# turbo_cache/usn_journal are mocked here (each already has its own full
# test suite, test_turbo_cache.py/test_usn_journal.py); these tests are
# specifically about turbo_scan's *decision* of which path to take and how
# it reacts to each failure mode -- test_turbo_scan_integration.py covers
# the real, unmocked, end-to-end wiring between all three modules.


@pytest.fixture(autouse=True)
def _no_real_cache_db(monkeypatch):
    """get_records_using_cache() calls turbo_cache.init_cache_db()
    unconditionally (cheap and idempotent in real use) -- the tests below
    mock every other turbo_cache/usn_journal call individually, so this
    just guards against any of them accidentally touching the real
    on-disk %LOCALAPPDATA% cache DB. Harmless no-op for every other test
    in this file, which never calls get_records_using_cache at all."""
    monkeypatch.setattr(turbo_scan.turbo_cache, "init_cache_db", lambda: None)


class _FakeRecordSource:
    def __init__(self, volume_serial=1, record_size=1024, record_count=3, raw_handle=999):
        self.volume_serial = volume_serial
        self.record_size = record_size
        self.record_count = record_count
        self.raw_handle = raw_handle

    def record_at(self, record_number):
        return b""  # mft_parser.parse_base_record is mocked in these tests


class _FakeParsedRecord:
    """Just enough of mft_parser.ParsedRecord's shape for the caching
    decision logic to touch (._full_scan_and_cache reads .frn to find the
    root record) -- content correctness is mft_parser/mft_scan's concern,
    already covered by their own test files."""

    def __init__(self, record_number):
        self.frn = record_number  # well under _FRN_RECORD_NUMBER_MASK, so frn == record_number here


def _fake_parsed_record(record_number):
    return _FakeParsedRecord(record_number)


def test_no_cache_yet_does_a_full_scan(monkeypatch):
    source = _FakeRecordSource(record_count=3)
    monkeypatch.setattr(turbo_scan.turbo_cache, "get_cached_volume", lambda serial: None)
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "save_full_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "ensure_journal",
        lambda handle: pytest.fail("no journal on a first scan in this test"),
    )

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert len(records) == 3  # one per record_number 0, 1, 2


def test_cancelling_mid_full_scan_raises_instead_of_returning_a_partial_list(monkeypatch):
    """scan_with_best_engine's own docstring promises Turbo Scan never
    hands back a partial tree on abort -- get_records_using_cache (via
    _full_scan_and_cache) must raise on cancellation, not silently return
    the records parsed so far, so the caller's existing any-failure
    fallback handles it the same as any other Turbo failure."""
    source = _FakeRecordSource(record_count=5)
    cancel_event = threading.Event()
    calls = []

    def fake_parse(record_number, record_source):
        calls.append(record_number)
        if record_number == 2:
            cancel_event.set()  # user hits Cancel partway through the volume
        return _fake_parsed_record(record_number)

    monkeypatch.setattr(turbo_scan.turbo_cache, "get_cached_volume", lambda serial: None)
    monkeypatch.setattr(turbo_scan.mft_parser, "parse_base_record", fake_parse)
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "save_full_scan",
        lambda *a, **k: pytest.fail("a cancelled scan must never be cached"),
    )

    with pytest.raises(RuntimeError):
        turbo_scan.get_records_using_cache(source, "C:\\", None, cancel_event)

    # Cancellation is checked at the top of each loop iteration, so the
    # record whose parse *set* cancel_event (2) is the last one processed;
    # record_count is 5, so records 3 and 4 must never even be attempted.
    assert calls == [0, 1, 2]


def test_cancelling_mid_incremental_refresh_raises_not_returns_none(monkeypatch):
    """_try_incremental_refresh's None return is reserved for 'no usable
    cursor' (get_records_using_cache's cue to do a full scan); conflating
    that with cancellation would send a cancelled scan straight into a
    full, wasted volume rescan instead of aborting immediately."""
    source = _FakeRecordSource(record_count=1)
    cancel_event = threading.Event()

    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 100,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7,
            first_usn=0,
            next_usn=200,
            lowest_valid_usn=0,
            max_usn=1000,
        ),
    )

    class _DirtyRecord:
        record_number = 7

    def fake_read_journal_changes(handle, journal_id, next_usn, lowest_valid_usn=None):
        cancel_event.set()  # user hits Cancel while applying the change set
        return [_DirtyRecord()], 201

    monkeypatch.setattr(turbo_scan.usn_journal, "read_journal_changes", fake_read_journal_changes)
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "apply_incremental_changes",
        lambda *a, **k: pytest.fail("a cancelled refresh must never write to the cache"),
    )
    monkeypatch.setattr(
        turbo_scan.mft_parser,
        "parse_base_record",
        lambda n, s: pytest.fail("should never reach the dirty-record parse loop"),
    )
    monkeypatch.setattr(
        turbo_scan,
        "_full_scan_and_cache",
        lambda *a, **k: pytest.fail("cancellation must raise, not fall through to a full rescan"),
    )

    with pytest.raises(RuntimeError):
        turbo_scan.get_records_using_cache(source, "C:\\", None, cancel_event)


def test_cache_with_no_journal_cursor_does_a_full_scan(monkeypatch):
    source = _FakeRecordSource(record_count=2)
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {"record_size": 1024, "next_usn": None},
    )
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "save_full_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "ensure_journal",
        lambda handle: pytest.fail("should not touch the journal"),
    )

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert len(records) == 2


def test_cache_with_valid_journal_and_no_changes_uses_incremental_path(monkeypatch):
    source = _FakeRecordSource()
    cached_records = [_fake_parsed_record(0), _fake_parsed_record(1)]
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 500,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7, first_usn=0, next_usn=500, lowest_valid_usn=0, max_usn=9999
        ),
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "read_journal_changes",
        lambda handle, journal_id, start_usn, lowest_valid_usn=None: ([], 500),
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "apply_incremental_changes", lambda *a, **k: None)
    monkeypatch.setattr(turbo_scan.turbo_cache, "load_all_records", lambda serial: cached_records)
    monkeypatch.setattr(
        turbo_scan.mft_parser,
        "parse_base_record",
        lambda n, s: pytest.fail("a full scan should not run on the incremental path"),
    )

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert records is cached_records


def test_incremental_refresh_posts_status_and_progress_messages(monkeypatch):
    # The core fix for "elevated Turbo Scan shows no progress at all" when
    # the scan turns out to be a cached incremental refresh -- previously
    # _try_incremental_refresh never took or posted to progress_q at all,
    # regardless of engine path (in-process or elevated-helper); this is a
    # real, separate gap from run_elevated_scan_windows's own
    # process-boundary relay fix, found via a real user report.
    source = _FakeRecordSource()
    cached_records = [_fake_parsed_record(0)]
    dirty = [usn_journal.DirtyRecord(record_number=n, reason=0x1) for n in range(250)]

    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 500,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7, first_usn=0, next_usn=500, lowest_valid_usn=0, max_usn=9999
        ),
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "read_journal_changes",
        lambda handle, journal_id, start_usn, lowest_valid_usn=None: (dirty, 999),
    )
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "apply_incremental_changes", lambda *a, **k: None)
    monkeypatch.setattr(turbo_scan.turbo_cache, "load_all_records", lambda serial: cached_records)

    progress_q = queue.Queue()
    records = turbo_scan.get_records_using_cache(source, "C:\\", progress_q, threading.Event())

    assert records is cached_records
    messages = []
    while not progress_q.empty():
        messages.append(progress_q.get_nowait())

    statuses = [payload for kind, payload in messages if kind == "status"]
    assert any("250" in s and "applying" in s.lower() for s in statuses)
    assert any("loading" in s.lower() for s in statuses)

    progress_counts = [payload for kind, payload in messages if kind == "progress"]
    assert progress_counts == [200]  # only one multiple of 200 within 250 dirty records


def test_incremental_refresh_with_no_dirty_records_skips_the_applying_status(monkeypatch):
    source = _FakeRecordSource()
    cached_records = [_fake_parsed_record(0)]

    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 500,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7, first_usn=0, next_usn=500, lowest_valid_usn=0, max_usn=9999
        ),
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "read_journal_changes",
        lambda handle, journal_id, start_usn, lowest_valid_usn=None: ([], 500),
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "apply_incremental_changes", lambda *a, **k: None)
    monkeypatch.setattr(turbo_scan.turbo_cache, "load_all_records", lambda serial: cached_records)

    progress_q = queue.Queue()
    turbo_scan.get_records_using_cache(source, "C:\\", progress_q, threading.Event())

    messages = []
    while not progress_q.empty():
        messages.append(progress_q.get_nowait())
    statuses = [payload for kind, payload in messages if kind == "status"]
    assert not any("applying" in s.lower() for s in statuses)  # nothing to apply
    assert any(
        "loading" in s.lower() for s in statuses
    )  # still posted -- load_all_records still runs


def test_journal_id_mismatch_falls_back_to_full_scan_and_invalidates(monkeypatch):
    source = _FakeRecordSource(record_count=1)
    invalidated = []
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 500,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=999, first_usn=0, next_usn=500, lowest_valid_usn=0, max_usn=9999
        ),
    )
    monkeypatch.setattr(
        turbo_scan.turbo_cache, "invalidate_volume", lambda serial: invalidated.append(serial)
    )
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "save_full_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "ensure_journal",
        lambda handle: (_ for _ in ()).throw(usn_journal.UsnJournalError("no journal")),
    )

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert len(records) == 1  # fell all the way through to a full scan
    assert invalidated == [1]


def test_corrupt_cached_record_falls_back_to_full_scan_and_invalidates(monkeypatch):
    """A corrupt cached_records blob must be treated the same as a stale
    USN journal: invalidate this volume's cache and fall through to a
    full scan, so the NEXT scan gets a clean rebuild instead of hitting
    the same corrupt row (and Compatible-engine fallback) forever."""
    source = _FakeRecordSource(record_count=1)
    invalidated = []
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 500,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7,
            first_usn=0,
            next_usn=500,
            lowest_valid_usn=0,
            max_usn=9999,
        ),
    )

    class _DirtyRecord:
        record_number = 3

    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "read_journal_changes",
        lambda handle, journal_id, next_usn, lowest_valid_usn=None: ([_DirtyRecord()], 501),
    )
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "apply_incremental_changes", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "load_all_records",
        lambda serial: (_ for _ in ()).throw(
            turbo_scan.turbo_cache.TurboCacheCorruptError("truncated blob")
        ),
    )
    monkeypatch.setattr(
        turbo_scan.turbo_cache, "invalidate_volume", lambda serial: invalidated.append(serial)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "save_full_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "ensure_journal",
        lambda handle: (_ for _ in ()).throw(usn_journal.UsnJournalError("no journal")),
    )

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert len(records) == 1  # fell all the way through to a full scan
    assert invalidated == [1]


def test_wrapped_journal_falls_back_to_full_scan(monkeypatch):
    source = _FakeRecordSource(record_count=1)
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        # cached next_usn (100) is below the journal's current lowest_valid_usn (200) -- wrapped
        lambda serial: {
            "record_size": 1024,
            "next_usn": 100,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7, first_usn=150, next_usn=500, lowest_valid_usn=200, max_usn=9999
        ),
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "invalidate_volume", lambda serial: None)
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )
    monkeypatch.setattr(turbo_scan.turbo_cache, "save_full_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "ensure_journal",
        lambda handle: (_ for _ in ()).throw(usn_journal.UsnJournalError("no journal")),
    )

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert len(records) == 1


def test_dirty_record_that_no_longer_parses_is_deleted_not_upserted(monkeypatch):
    source = _FakeRecordSource()
    applied = {}
    monkeypatch.setattr(
        turbo_scan.turbo_cache,
        "get_cached_volume",
        lambda serial: {
            "record_size": 1024,
            "next_usn": 500,
            "usn_journal_id": 7,
            "volume_serial": 1,
        },
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "query_journal",
        lambda handle: usn_journal.JournalState(
            journal_id=7, first_usn=0, next_usn=500, lowest_valid_usn=0, max_usn=9999
        ),
    )
    monkeypatch.setattr(
        turbo_scan.usn_journal,
        "read_journal_changes",
        lambda handle, journal_id, start_usn, lowest_valid_usn=None: (
            [usn_journal.DirtyRecord(record_number=42, reason=0x200)],
            510,
        ),
    )
    # record 42 no longer parses -- it was deleted since the cache was built
    monkeypatch.setattr(turbo_scan.mft_parser, "parse_base_record", lambda n, s: None)

    def fake_apply(volume_serial, upserts, deletes, new_next_usn):
        applied["upserts"] = upserts
        applied["deletes"] = deletes
        applied["new_next_usn"] = new_next_usn

    monkeypatch.setattr(turbo_scan.turbo_cache, "apply_incremental_changes", fake_apply)
    monkeypatch.setattr(turbo_scan.turbo_cache, "load_all_records", lambda serial: [])

    turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert applied["upserts"] == []
    assert applied["deletes"] == [42]
    assert applied["new_next_usn"] == 510


def test_cache_write_failure_after_a_full_scan_does_not_lose_the_scan_result(monkeypatch):
    # record_count=6 so record #5 (the root, per mft_scan's own MFT-record-#5
    # invariant) exists -- otherwise _full_scan_and_cache never even
    # attempts to cache, and this test wouldn't exercise the failure path.
    source = _FakeRecordSource(record_count=6)
    monkeypatch.setattr(turbo_scan.turbo_cache, "get_cached_volume", lambda serial: None)
    monkeypatch.setattr(
        turbo_scan.mft_parser, "parse_base_record", lambda n, s: _fake_parsed_record(n)
    )

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(turbo_scan.turbo_cache, "save_full_scan", boom)

    records = turbo_scan.get_records_using_cache(source, "C:\\", None, threading.Event())

    assert len(records) == 6  # caching failed silently; the scan's own result is untouched
