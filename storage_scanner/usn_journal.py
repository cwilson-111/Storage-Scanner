"""Reads the NTFS USN Change Journal: the list of files that changed on a
volume since a saved resume point (a "USN"), used by storage_scanner.
turbo_scan's incremental-refresh path to avoid re-reading every MFT
record on a repeat Turbo Scan.

Every call here works against a read-only volume handle (see
mft_volume.RecordSource.raw_handle) -- confirmed necessary on a real
machine, not just tidy: an earlier version needed FSCTL_CREATE_USN_JOURNAL's
write-access requirement to open the shared handle with GENERIC_READ |
GENERIC_WRITE, and that got the whole handle open blocked outright by
FortiClient (this user's AV/EDR), breaking every Turbo Scan, not just
journal creation. See create_journal()/ensure_journal()'s docstrings.

Deliberately thin, mirroring mft_volume.py's split: this module only knows
how to talk to the three USN-related FSCTL codes and hand back which MFT
record *numbers* changed (plus their raw Reason bitmask, kept for logging
only -- see read_journal_changes). It never decides what a change *means*;
the caller re-parses each dirty record fresh via mft_parser.parse_base_record
and lets that be the source of truth, rather than this module trying to
hand-interpret CREATE/DELETE/RENAME_OLD_NAME/RENAME_NEW_NAME/etc. itself.

Dirty entries are keyed by MFT record *number*, not by the packed FRN a
USN_RECORD's FileReferenceNumber actually carries -- that FRN includes
whatever sequence number was current at change time, which goes stale the
instant a record slot is freed and reused for a different file. Re-parsing
by record number and letting the fresh ParsedRecord.frn (with its current,
correct sequence number) become the new cache key handles a reused slot with
no special-case code anywhere.

Every real DeviceIoControl call here can only be exercised end-to-end on a
real, elevated Windows session against a real NTFS volume with an active
journal -- same caveat mft_volume.py's own module docstring makes about its
CreateFileW/ReadFile calls. What's unit-tested in tests/test_usn_journal.py
against a faked ctypes.windll.kernel32 is this module's own buffer-parsing
and control-flow logic against the documented Win32 struct layouts, not
real journal behavior.
"""

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass

from storage_scanner.mft_parser import _FRN_RECORD_NUMBER_MASK

_FSCTL_QUERY_USN_JOURNAL = 0x000900F4
_FSCTL_CREATE_USN_JOURNAL = 0x000900E7
_FSCTL_READ_USN_JOURNAL = 0x000900BB

_USN_REASON_ALL = 0xFFFFFFFF
# Coalesce every intermediate change into one record per handle-close,
# rather than a flood of DATA_EXTEND/DATA_TRUNCATION records mid-write --
# all this module needs is "this record changed," not a blow-by-blow.
_RETURN_ONLY_ON_CLOSE = 1

# Large enough to hold many USN_RECORDs per DeviceIoControl call without
# needing a syscall per change; not tied to any on-disk structure size.
_READ_BUFFER_BYTES = 64 * 1024

# -- USN_RECORD_V2 fixed header (60 bytes; FileName, if present, follows at
# -- FileNameOffset -- never read here, only used to skip via RecordLength) --
_USN_RECORD_HEADER_FORMAT = "<IHHQQqqIIIIHH"
_USN_RECORD_HEADER_SIZE = struct.calcsize(_USN_RECORD_HEADER_FORMAT)  # 60


class UsnJournalError(Exception):
    """Any failure querying/creating/reading the USN journal -- no journal
    exists yet, the journal ID no longer matches (deleted/recreated since a
    saved cursor), the journal has wrapped past a saved cursor, or the
    DeviceIoControl call itself failed. Always caught by the caller's
    incremental-refresh attempt and treated as "fall back to a full Turbo
    Scan," same as every other Turbo Scan failure mode -- never surfaced
    raw to a user."""


@dataclass
class JournalState:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int
    max_usn: int


@dataclass
class DirtyRecord:
    record_number: int
    reason: int  # raw Reason bitmask, OR'd across every USN entry seen for
                 # this record number -- logging/debugging only, never
                 # branched on (see module docstring).


class _USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("UsnJournalID", ctypes.c_uint64),
        ("FirstUsn", ctypes.c_int64),
        ("NextUsn", ctypes.c_int64),
        ("LowestValidUsn", ctypes.c_int64),
        ("MaxUsn", ctypes.c_int64),
        ("MaximumSize", ctypes.c_uint64),
        ("AllocationDelta", ctypes.c_uint64),
    ]


class _CREATE_USN_JOURNAL_DATA(ctypes.Structure):
    _fields_ = [
        ("MaximumSize", ctypes.c_uint64),
        ("AllocationDelta", ctypes.c_uint64),
    ]


class _READ_USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartUsn", ctypes.c_int64),
        ("ReasonMask", wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout", ctypes.c_uint64),
        ("BytesToWaitFor", ctypes.c_uint64),
        ("UsnJournalID", ctypes.c_uint64),
    ]


def query_journal(handle):
    """FSCTL_QUERY_USN_JOURNAL. Raises UsnJournalError if no journal exists
    on this volume yet (the caller decides whether to create one via
    create_journal/ensure_journal)."""
    buffer = _USN_JOURNAL_DATA_V0()
    bytes_returned = wintypes.DWORD(0)
    succeeded = ctypes.windll.kernel32.DeviceIoControl(
        handle, _FSCTL_QUERY_USN_JOURNAL, None, 0,
        ctypes.byref(buffer), ctypes.sizeof(buffer),
        ctypes.byref(bytes_returned), None,
    )
    if not succeeded:
        raise UsnJournalError("FSCTL_QUERY_USN_JOURNAL failed (no journal on this volume?)")
    return JournalState(
        journal_id=buffer.UsnJournalID,
        first_usn=buffer.FirstUsn,
        next_usn=buffer.NextUsn,
        lowest_valid_usn=buffer.LowestValidUsn,
        max_usn=buffer.MaxUsn,
    )


def create_journal(handle):
    """FSCTL_CREATE_USN_JOURNAL with MaximumSize=0, AllocationDelta=0 (let
    Windows choose its own defaults).

    Needs write access to the volume handle per documented Win32
    semantics -- `handle` here is nonetheless the same read-only handle
    every other call in this module uses (see mft_volume.RecordSource.
    raw_handle), so on a volume lacking a journal already this simply
    fails with an OS-level access-denied, same as any other
    FSCTL_CREATE_USN_JOURNAL failure. That's deliberate: an earlier
    version opened the shared handle with GENERIC_READ | GENERIC_WRITE
    specifically so this call could succeed even on a volume with no
    journal yet, and that broke Turbo Scan outright on a real machine --
    FortiClient (this user's AV/EDR) blocked opening the volume at all
    once write access was requested, not just this one FSCTL call. Only
    ever called from ensure_journal(), and only when query_journal()
    already confirmed no journal exists -- see there.
    """
    input_data = _CREATE_USN_JOURNAL_DATA(MaximumSize=0, AllocationDelta=0)
    bytes_returned = wintypes.DWORD(0)
    succeeded = ctypes.windll.kernel32.DeviceIoControl(
        handle, _FSCTL_CREATE_USN_JOURNAL,
        ctypes.byref(input_data), ctypes.sizeof(input_data),
        None, 0, ctypes.byref(bytes_returned), None,
    )
    if not succeeded:
        raise UsnJournalError("FSCTL_CREATE_USN_JOURNAL failed")


def ensure_journal(handle):
    """Return the current JournalState for this volume -- what a caller
    wants right after a full scan finishes, to establish the first resume
    cursor. Tries query_journal() first (read-only, and the common case:
    any real, actively-used Windows system almost always already has an
    active USN journal -- Search indexing, System Restore, OneDrive, etc.
    all keep one running) and only falls through to create_journal() if
    none exists yet, since that needs write access this handle may not
    have (see create_journal's docstring for why that's on purpose)."""
    try:
        return query_journal(handle)
    except UsnJournalError:
        pass
    create_journal(handle)
    return query_journal(handle)


def read_journal_changes(handle, journal_id, start_usn, lowest_valid_usn=None):
    """Read every change recorded since `start_usn`, repeatedly issuing
    FSCTL_READ_USN_JOURNAL until caught up to the journal's current head.

    Returns (dirty_records, new_next_usn): `dirty_records` is a list of
    DirtyRecord, one per distinct MFT record number touched (deduplicated
    -- a record touched multiple times between two scans only needs one
    re-parse), and `new_next_usn` is the cursor to save for next time.

    Raises UsnJournalError if any DeviceIoControl call fails -- including
    a journal-ID mismatch (the journal was deleted and recreated since
    `journal_id` was captured), which the underlying FSCTL call itself
    rejects rather than this function detecting it separately.

    `lowest_valid_usn`, if given, is checked against `start_usn` before
    any journal I/O: if the journal has wrapped/been purged past
    `start_usn`, silently reading from here on would only see records
    that survived the purge, missing every change in the gap, with
    nothing to signal that a full rescan is actually needed instead. The
    caller (turbo_scan._try_incremental_refresh) already checks this
    itself from its own freshly-queried JournalState before calling here
    -- this is a second, self-contained check so the function can't
    silently misbehave for some future caller that forgets to.
    """
    if lowest_valid_usn is not None and start_usn < lowest_valid_usn:
        raise UsnJournalError(
            f"start_usn ({start_usn}) is below the journal's lowest_valid_usn "
            f"({lowest_valid_usn}) -- the journal has wrapped past this cursor"
        )

    dirty_by_record = {}
    out_buffer = ctypes.create_string_buffer(_READ_BUFFER_BYTES)
    current_usn = start_usn

    while True:
        input_data = _READ_USN_JOURNAL_DATA_V0(
            StartUsn=current_usn, ReasonMask=_USN_REASON_ALL,
            ReturnOnlyOnClose=_RETURN_ONLY_ON_CLOSE, Timeout=0,
            BytesToWaitFor=0, UsnJournalID=journal_id,
        )
        bytes_returned = wintypes.DWORD(0)
        succeeded = ctypes.windll.kernel32.DeviceIoControl(
            handle, _FSCTL_READ_USN_JOURNAL,
            ctypes.byref(input_data), ctypes.sizeof(input_data),
            out_buffer, _READ_BUFFER_BYTES,
            ctypes.byref(bytes_returned), None,
        )
        if not succeeded:
            raise UsnJournalError(
                f"FSCTL_READ_USN_JOURNAL failed reading from USN {current_usn} "
                f"(journal recreated/deleted, or volume dismounted?)"
            )

        data = out_buffer.raw[:bytes_returned.value]
        if len(data) < 8:
            raise UsnJournalError(
                f"FSCTL_READ_USN_JOURNAL returned an implausibly short buffer "
                f"({len(data)} bytes)"
            )
        next_usn = struct.unpack_from("<q", data, 0)[0]

        offset = 8
        found_any = False
        while offset + _USN_RECORD_HEADER_SIZE <= len(data):
            (record_length, _major, _minor, file_ref, _parent_ref, _usn,
             _timestamp, reason, _source_info, _security_id, _file_attrs,
             _name_len, _name_offset) = struct.unpack_from(
                _USN_RECORD_HEADER_FORMAT, data, offset
            )
            if record_length == 0:
                break
            record_number = file_ref & _FRN_RECORD_NUMBER_MASK
            dirty_by_record[record_number] = dirty_by_record.get(record_number, 0) | reason
            found_any = True
            offset += record_length

        if not found_any:
            dirty_records = [
                DirtyRecord(record_number=record_number, reason=reason)
                for record_number, reason in dirty_by_record.items()
            ]
            return dirty_records, next_usn

        current_usn = next_usn
