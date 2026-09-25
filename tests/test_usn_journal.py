"""Tests for storage_scanner.usn_journal against a faked ctypes.windll.
kernel32 -- the same fake-the-Win32-call technique tests/test_mft_volume.py
already uses, with DeviceIoControl dispatching on the FSCTL code (three
different USN-related codes share one entry point on a real volume handle).

Every USN_RECORD fixture here is hand-packed via struct.pack against the
documented on-disk V2 layout (not pre-parsed DirtyRecord objects), so these
are a genuine check of the buffer-walking logic against the real wire
format -- the same "hand-build real bytes" discipline test_mft_parser.py
and test_mft_volume.py already follow, which is what caught this project's
real parsing bugs. What these tests do NOT and cannot prove is that real
FSCTL_QUERY_USN_JOURNAL/FSCTL_CREATE_USN_JOURNAL/FSCTL_READ_USN_JOURNAL
calls behave this way against a real, live journal -- that needs a real
elevated Windows session (not yet exercised; this module isn't wired into
any live scan path yet).
"""

import ctypes
import struct
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import usn_journal
from storage_scanner.mft_parser import _pack_frn
from storage_scanner.usn_journal import UsnJournalError, read_journal_changes

_FAKE_HANDLE = 424242


def _pack_usn_record(file_ref, parent_ref, usn, reason, record_length=None):
    length = record_length if record_length is not None else usn_journal._USN_RECORD_HEADER_SIZE
    return struct.pack(
        usn_journal._USN_RECORD_HEADER_FORMAT,
        length,
        2,
        0,  # RecordLength, MajorVersion, MinorVersion
        file_ref,
        parent_ref,
        usn,
        0,  # FileReferenceNumber, ParentFRN, Usn, TimeStamp
        reason,
        0,
        0,
        0,  # Reason, SourceInfo, SecurityId, FileAttributes
        0,
        usn_journal._USN_RECORD_HEADER_SIZE,  # FileNameLength, FileNameOffset
    )


def _read_response(next_usn, records_bytes=b""):
    return struct.pack("<q", next_usn) + records_bytes


class _FakeUsnKernel32:
    def __init__(
        self,
        *,
        journal_id=1,
        first_usn=0,
        next_usn=100,
        lowest_valid_usn=0,
        max_usn=10_000,
        query_fails=False,
        create_fails=False,
        read_responses=None,
    ):
        self.journal_id = journal_id
        self.first_usn = first_usn
        self.next_usn = next_usn
        self.lowest_valid_usn = lowest_valid_usn
        self.max_usn = max_usn
        self.query_fails = query_fails
        self.create_fails = create_fails
        self.read_responses = list(read_responses or [])
        self.query_calls = 0
        self.create_calls = 0
        self.last_create_data = None
        self.read_start_usns = []

    def DeviceIoControl(
        self, handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped
    ):
        if code == usn_journal._FSCTL_QUERY_USN_JOURNAL:
            self.query_calls += 1
            if self.query_fails:
                return 0
            info = ctypes.cast(out_ref, ctypes.POINTER(usn_journal._USN_JOURNAL_DATA_V0)).contents
            info.UsnJournalID = self.journal_id
            info.FirstUsn = self.first_usn
            info.NextUsn = self.next_usn
            info.LowestValidUsn = self.lowest_valid_usn
            info.MaxUsn = self.max_usn
            info.MaximumSize = 0
            info.AllocationDelta = 0
            return 1

        if code == usn_journal._FSCTL_CREATE_USN_JOURNAL:
            self.create_calls += 1
            data = ctypes.cast(
                in_buf, ctypes.POINTER(usn_journal._CREATE_USN_JOURNAL_DATA)
            ).contents
            self.last_create_data = (data.MaximumSize, data.AllocationDelta)
            if self.create_fails:
                return 0
            return 1

        if code == usn_journal._FSCTL_READ_USN_JOURNAL:
            request = ctypes.cast(
                in_buf, ctypes.POINTER(usn_journal._READ_USN_JOURNAL_DATA_V0)
            ).contents
            self.read_start_usns.append(request.StartUsn)
            if request.UsnJournalID != self.journal_id:
                return 0  # journal ID mismatch -- the real FSCTL rejects this outright
            if not self.read_responses:
                raise AssertionError("test ran out of scripted read_responses")
            payload = self.read_responses.pop(0)
            out_ref.raw = payload.ljust(out_size, b"\x00")
            ctypes.cast(bytes_ret_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = len(payload)
            return 1

        raise AssertionError(f"unexpected FSCTL code {code:#x}")


class _FakeWinDLL:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def _patch(monkeypatch, kernel32):
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)


# -- query_journal ------------------------------------------------------- #


def test_query_journal_parses_the_returned_buffer(monkeypatch):
    kernel32 = _FakeUsnKernel32(
        journal_id=99, first_usn=10, next_usn=500, lowest_valid_usn=5, max_usn=99_999
    )
    _patch(monkeypatch, kernel32)

    state = usn_journal.query_journal(_FAKE_HANDLE)

    assert state.journal_id == 99
    assert state.first_usn == 10
    assert state.next_usn == 500
    assert state.lowest_valid_usn == 5
    assert state.max_usn == 99_999


def test_query_journal_raises_when_no_journal_exists(monkeypatch):
    kernel32 = _FakeUsnKernel32(query_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(UsnJournalError):
        usn_journal.query_journal(_FAKE_HANDLE)


# -- create_journal / ensure_journal -------------------------------------- #


def test_create_journal_sends_zero_maximum_size_and_allocation_delta(monkeypatch):
    kernel32 = _FakeUsnKernel32()
    _patch(monkeypatch, kernel32)

    usn_journal.create_journal(_FAKE_HANDLE)

    assert kernel32.create_calls == 1
    assert kernel32.last_create_data == (0, 0)


def test_create_journal_raises_on_failure(monkeypatch):
    kernel32 = _FakeUsnKernel32(create_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(UsnJournalError):
        usn_journal.create_journal(_FAKE_HANDLE)


def test_ensure_journal_never_creates_when_one_already_exists(monkeypatch):
    # The common, real-world case: any actively-used Windows system almost
    # always already has a journal. ensure_journal must not attempt
    # FSCTL_CREATE_USN_JOURNAL here -- confirmed on a real machine that
    # requesting write access just to make that call possible gets the
    # whole volume-open blocked by AV/EDR software, so this path has to
    # stay read-only-only whenever it possibly can.
    kernel32 = _FakeUsnKernel32(journal_id=7, next_usn=42)
    _patch(monkeypatch, kernel32)

    state = usn_journal.ensure_journal(_FAKE_HANDLE)

    assert kernel32.create_calls == 0
    assert kernel32.query_calls == 1
    assert state.journal_id == 7
    assert state.next_usn == 42


def test_ensure_journal_creates_one_only_when_none_exists_yet(monkeypatch):
    kernel32 = _FakeUsnKernel32(journal_id=7, next_usn=42, query_fails=True)
    _patch(monkeypatch, kernel32)
    # query_fails only affects the *first* query call in this test --
    # simulate "no journal yet" on attempt 1, then "just created it" from
    # attempt 2 onward (create_journal doesn't change the fake's state,
    # so toggle query_fails off once create_journal has been called).
    real_create_journal = usn_journal.create_journal

    def create_then_allow_query(handle):
        real_create_journal(handle)
        kernel32.query_fails = False

    monkeypatch.setattr(usn_journal, "create_journal", create_then_allow_query)

    state = usn_journal.ensure_journal(_FAKE_HANDLE)

    assert kernel32.create_calls == 1
    assert kernel32.query_calls == 2  # the initial failing probe, then the real one
    assert state.journal_id == 7
    assert state.next_usn == 42


# -- read_journal_changes -------------------------------------------------- #


def test_read_journal_changes_returns_dirty_records_and_new_cursor(monkeypatch):
    records = _pack_usn_record(
        _pack_frn(1, 100), _pack_frn(1, 5), usn=200, reason=0x100
    ) + _pack_usn_record(_pack_frn(1, 101), _pack_frn(1, 5), usn=210, reason=0x200)
    kernel32 = _FakeUsnKernel32(
        journal_id=1,
        read_responses=[
            _read_response(next_usn=300, records_bytes=records),
            _read_response(next_usn=300),  # caught up: no more records
        ],
    )
    _patch(monkeypatch, kernel32)

    dirty, new_next_usn = read_journal_changes(_FAKE_HANDLE, journal_id=1, start_usn=100)

    assert new_next_usn == 300
    assert {d.record_number for d in dirty} == {100, 101}
    assert {d.record_number: d.reason for d in dirty} == {100: 0x100, 101: 0x200}


def test_read_journal_changes_deduplicates_a_record_touched_multiple_times(monkeypatch):
    records = _pack_usn_record(
        _pack_frn(1, 100), _pack_frn(1, 5), usn=200, reason=0x100
    ) + _pack_usn_record(  # DATA_OVERWRITE
        _pack_frn(1, 100), _pack_frn(1, 5), usn=205, reason=0x1000
    )  # RENAME_OLD_NAME
    kernel32 = _FakeUsnKernel32(
        journal_id=1,
        read_responses=[
            _read_response(next_usn=300, records_bytes=records),
            _read_response(next_usn=300),  # caught up: no more records
        ],
    )
    _patch(monkeypatch, kernel32)

    dirty, _new_next_usn = read_journal_changes(_FAKE_HANDLE, journal_id=1, start_usn=100)

    assert len(dirty) == 1
    assert dirty[0].record_number == 100
    assert dirty[0].reason == (0x100 | 0x1000)  # both reasons OR'd together


def test_read_journal_changes_masks_the_sequence_number_out_of_the_frn(monkeypatch):
    # A nonzero packed sequence number in FileReferenceNumber must not leak
    # into the returned record_number -- this is the whole point of keying
    # the cache by record number, not by FRN (see module docstring).
    high_sequence_frn = _pack_frn(0x1234, 555)
    records = _pack_usn_record(high_sequence_frn, _pack_frn(1, 5), usn=200, reason=0x100)
    kernel32 = _FakeUsnKernel32(
        journal_id=1,
        read_responses=[
            _read_response(next_usn=300, records_bytes=records),
            _read_response(next_usn=300),  # caught up: no more records
        ],
    )
    _patch(monkeypatch, kernel32)

    dirty, _new_next_usn = read_journal_changes(_FAKE_HANDLE, journal_id=1, start_usn=100)

    assert len(dirty) == 1
    assert dirty[0].record_number == 555


def test_read_journal_changes_pages_until_no_records_are_returned(monkeypatch):
    first_page_records = _pack_usn_record(_pack_frn(1, 100), _pack_frn(1, 5), usn=200, reason=0x100)
    second_page_records = _pack_usn_record(
        _pack_frn(1, 101), _pack_frn(1, 5), usn=250, reason=0x100
    )
    kernel32 = _FakeUsnKernel32(
        journal_id=1,
        read_responses=[
            _read_response(next_usn=260, records_bytes=first_page_records),
            _read_response(next_usn=310, records_bytes=second_page_records),
            _read_response(next_usn=310, records_bytes=b""),  # caught up: no records
        ],
    )
    _patch(monkeypatch, kernel32)

    dirty, new_next_usn = read_journal_changes(_FAKE_HANDLE, journal_id=1, start_usn=100)

    assert kernel32.read_start_usns == [100, 260, 310]
    assert new_next_usn == 310
    assert {d.record_number for d in dirty} == {100, 101}


def test_read_journal_changes_raises_on_journal_id_mismatch(monkeypatch):
    kernel32 = _FakeUsnKernel32(journal_id=1, read_responses=[_read_response(next_usn=300)])
    _patch(monkeypatch, kernel32)

    with pytest.raises(UsnJournalError):
        read_journal_changes(_FAKE_HANDLE, journal_id=999, start_usn=100)


def test_read_journal_changes_raises_when_start_usn_is_below_lowest_valid(monkeypatch):
    """Defense in depth: even though the sole caller (turbo_read.
    _try_incremental_scan) already checks this itself before calling
    in, read_journal_changes must refuse a start_usn the journal has
    wrapped past on its own, rather than silently reading only the
    surviving post-gap records and missing everything purged in between.
    No DeviceIoControl call should even happen -- the fake kernel32 has
    no read_responses configured, so a call would raise IndexError/error
    on its own if this check didn't short-circuit first.
    """
    kernel32 = _FakeUsnKernel32(journal_id=1, read_responses=[])
    _patch(monkeypatch, kernel32)

    with pytest.raises(UsnJournalError):
        read_journal_changes(_FAKE_HANDLE, journal_id=1, start_usn=100, lowest_valid_usn=200)


def test_read_journal_changes_proceeds_when_start_usn_is_at_or_above_lowest_valid(monkeypatch):
    kernel32 = _FakeUsnKernel32(journal_id=1, read_responses=[_read_response(next_usn=300)])
    _patch(monkeypatch, kernel32)

    dirty, new_next_usn = read_journal_changes(
        _FAKE_HANDLE,
        journal_id=1,
        start_usn=200,
        lowest_valid_usn=200,
    )

    assert dirty == []
    assert new_next_usn == 300
