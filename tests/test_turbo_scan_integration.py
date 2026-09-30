"""End-to-end integration test for Turbo Scan: a small fake NTFS volume
(built from real record bytes, not pre-parsed fixtures) is scanned through
the *entire* real pipeline -- storage_scanner.turbo_scan.scan_with_best_engine
-> drive_info.is_ntfs_fixed_drive -> mft_volume.RecordSource ->
mft_parser.parse_base_record -> mft_scan.build_tree -> find_subtree_node --
via a faked ctypes.windll, with only IS_ROOT forced True (the already-
elevated in-process shortcut) so no subprocess/UAC is involved.

Every phase already has its own focused unit tests with mocks at that
phase's own boundary (test_mft_parser.py, test_mft_scan.py,
test_mft_volume.py, test_drive_info.py, test_turbo_scan.py). This test
exists specifically to catch wiring mistakes *between* those phases that
per-phase mocking can't see -- e.g. a mismatched keyword argument name, or
a subtly wrong assumption about what one phase hands the next.
"""

import ctypes
import queue
import struct
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import drive_info, mft_volume, turbo_cache, turbo_scan, usn_journal
from storage_scanner.mft_parser import _pack_frn
from storage_scanner.models import Node
from storage_scanner.turbo_read import MftRead

pytestmark = pytest.mark.windows  # Windows-only feature

_RECORD_SIZE = 1024
_SECTOR_SIZE = 512
_USA_OFFSET = 48
_USA_SIZE = _RECORD_SIZE // _SECTOR_SIZE + 1
_FIRST_ATTR_OFFSET = ((_USA_OFFSET + _USA_SIZE * 2) + 7) // 8 * 8
_BYTES_PER_CLUSTER = 4096
_MFT_BYTE_OFFSET = 8192  # a couple of clusters of filler ahead of the "$MFT"

_ATTR_STANDARD_INFORMATION = 0x10
_ATTR_FILE_NAME = 0x30
_ATTR_DATA = 0x80
_ATTR_REPARSE_POINT = 0xC0
_ATTR_END_MARKER = 0xFFFFFFFF
_RECORD_FLAG_IN_USE = 0x0001
_RECORD_FLAG_IS_DIRECTORY = 0x0002


def _resident_attr(attr_type, value, attribute_id):
    header_len = 16 + 8
    total_len = ((header_len + len(value) + 7) // 8) * 8
    common = struct.pack("<IIBBHHH", attr_type, total_len, 0, 0, 0, 0, attribute_id)
    resident = struct.pack("<IHBB", len(value), header_len, 0, 0)
    body = bytearray(common + resident + value)
    body.extend(b"\x00" * (total_len - len(body)))
    return bytes(body)


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _std_info_value(file_attributes=0):
    return struct.pack("<QQQQI", 0, 0, 0, 0, file_attributes)


def _file_name_value(parent_frn, name):
    fixed = struct.pack("<QQQQQQQII", parent_frn, 0, 0, 0, 0, 0, 0, 0, 0)
    name_bytes = name.encode("utf-16-le")
    return fixed + bytes([len(name), 1]) + name_bytes  # namespace 1 = Win32


def _minimal_unsigned_bytes(value):
    n = 1
    while True:
        try:
            return value.to_bytes(n, "little", signed=False)
        except OverflowError:
            n += 1


def _minimal_signed_bytes(value):
    n = 1
    while True:
        try:
            return value.to_bytes(n, "little", signed=True)
        except OverflowError:
            n += 1


def _encode_runs(extents):
    """`extents` is a list of (length_clusters, absolute_lcn) tuples."""
    out = bytearray()
    previous_lcn = 0
    for length, lcn in extents:
        delta = lcn - previous_lcn
        previous_lcn = lcn
        length_bytes = _minimal_unsigned_bytes(length)
        delta_bytes = _minimal_signed_bytes(delta)
        out.append((len(delta_bytes) << 4) | len(length_bytes))
        out.extend(length_bytes)
        out.extend(delta_bytes)
    out.append(0)
    return bytes(out)


def _nonresident_data_attr(runs_bytes, attribute_id=2):
    common_len = 16
    nrh_len = 48
    data_runs_offset = common_len + nrh_len
    unpadded = data_runs_offset + len(runs_bytes)
    total_len = (unpadded + 7) // 8 * 8
    common = struct.pack("<IIBBHHH", _ATTR_DATA, total_len, 1, 0, 0, 0, attribute_id)
    nrh = struct.pack("<QQHHIQQQ", 0, 0, data_runs_offset, 0, 0, 0, 0, 0)
    body = bytearray(common + nrh)
    body.extend(runs_bytes)
    body.extend(b"\x00" * (total_len - len(body)))
    return bytes(body)


def _build_mft_record0(extents):
    """The real $MFT record #0 -- RecordSource now bootstraps the $MFT's
    actual (possibly multi-extent) physical layout from this record's own
    $DATA data runs, rather than assuming one contiguous span (that
    assumption was a real bug -- see mft_volume.py's module docstring).
    No $STANDARD_INFORMATION/$FILE_NAME needed: resolving extents only
    ever calls mft_parser.get_nonresident_data_runs_bytes."""
    data_attr = _nonresident_data_attr(_encode_runs(extents))
    attrs = data_attr + struct.pack("<I", _ATTR_END_MARKER)
    bytes_in_use = _FIRST_ATTR_OFFSET + len(attrs)
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE",
        _USA_OFFSET,
        _USA_SIZE,
        0,
        1,
        1,
        _FIRST_ATTR_OFFSET,
        _RECORD_FLAG_IN_USE,
        bytes_in_use,
        _RECORD_SIZE,
        0,
        0,
        0,
        0,
    )
    buf = bytearray(_RECORD_SIZE)
    buf[0 : len(header)] = header
    buf[_FIRST_ATTR_OFFSET : _FIRST_ATTR_OFFSET + len(attrs)] = attrs
    return _stamp_fixups(bytes(buf))


def _stamp_fixups(record):
    record = bytearray(record)
    usn = b"\x01\x00"
    record[_USA_OFFSET : _USA_OFFSET + 2] = usn
    for i in range(1, _USA_SIZE):
        sector_end = i * _SECTOR_SIZE - 2
        original = bytes(record[sector_end : sector_end + 2])
        record[_USA_OFFSET + 2 * i : _USA_OFFSET + 2 * i + 2] = original
        record[sector_end : sector_end + 2] = usn
    return bytes(record)


def _build_record(
    record_number,
    *,
    is_directory,
    sequence_number,
    file_name=None,
    file_names=None,
    data=None,
    file_attributes=0,
    reparse_tag=None,
):
    """`file_name` is a convenience for the common single-name case;
    `file_names` (a list) supports a genuinely hard-linked record with
    more than one $FILE_NAME attribute, one per parent directory.
    `reparse_tag` makes it a reparse point carrying that tag."""
    if file_names is None:
        file_names = [file_name]
    if reparse_tag is not None:
        file_attributes |= _FILE_ATTRIBUTE_REPARSE_POINT
    attrs = bytearray()
    attrs += _resident_attr(_ATTR_STANDARD_INFORMATION, _std_info_value(file_attributes), 0)
    for i, name_value in enumerate(file_names):
        attrs += _resident_attr(_ATTR_FILE_NAME, name_value, 1 + i)
    if data is not None:
        attrs += _resident_attr(_ATTR_DATA, data, 1 + len(file_names))
    if reparse_tag is not None:
        reparse_value = struct.pack("<IHH", reparse_tag, 0, 0)
        attrs += _resident_attr(_ATTR_REPARSE_POINT, reparse_value, 2 + len(file_names))
    attrs += struct.pack("<I", _ATTR_END_MARKER)

    flags = _RECORD_FLAG_IN_USE | (_RECORD_FLAG_IS_DIRECTORY if is_directory else 0)
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE",
        _USA_OFFSET,
        _USA_SIZE,
        0,
        sequence_number,
        1,
        _FIRST_ATTR_OFFSET,
        flags,
        _FIRST_ATTR_OFFSET + len(attrs),
        _RECORD_SIZE,
        0,
        0,
        0,
        record_number,
    )
    buf = bytearray(_RECORD_SIZE)
    buf[0 : len(header)] = header
    buf[_FIRST_ATTR_OFFSET : _FIRST_ATTR_OFFSET + len(attrs)] = attrs
    return _stamp_fixups(bytes(buf))


def _unused_record(record_number):
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE",
        _USA_OFFSET,
        _USA_SIZE,
        0,
        1,
        0,
        _FIRST_ATTR_OFFSET,
        0,
        _FIRST_ATTR_OFFSET,
        _RECORD_SIZE,  # flags=0: not in use
        0,
        0,
        0,
        record_number,
    )
    buf = bytearray(_RECORD_SIZE)
    buf[0 : len(header)] = header
    return _stamp_fixups(bytes(buf))


_MFT_START_LCN = _MFT_BYTE_OFFSET // _BYTES_PER_CLUSTER
_RECORDS_PER_CLUSTER = _BYTES_PER_CLUSTER // _RECORD_SIZE  # 4


def _build_fake_volume():
    """Record 0: the real $MFT record (its data runs describe one extent,
    at _MFT_START_LCN, covering every other record below). 1-4: unused
    placeholders. 5: root. 6: hello.txt (under root). 7: Sub (directory,
    under root). 8: inside.txt (under Sub). 9-11: implicit zero padding
    out to a whole number of clusters (harmlessly parsed as unused slots,
    same as any real MFT's free space)."""
    root_frn = _pack_frn(1, 5)
    sub_frn = _pack_frn(1, 7)

    total_records = 9  # 0..8
    length_clusters = -(-total_records // _RECORDS_PER_CLUSTER)  # ceil division
    record0 = _build_mft_record0([(length_clusters, _MFT_START_LCN)])

    records = [record0] + [_unused_record(n) for n in range(1, 5)]
    records.append(
        _build_record(
            5, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, ".")
        )
    )
    records.append(
        _build_record(
            6,
            is_directory=False,
            sequence_number=1,
            file_name=_file_name_value(root_frn, "hello.txt"),
            data=b"hi!",
        )
    )
    records.append(
        _build_record(
            7, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, "Sub")
        )
    )
    records.append(
        _build_record(
            8,
            is_directory=False,
            sequence_number=1,
            file_name=_file_name_value(sub_frn, "inside.txt"),
            data=b"xyz12",
        )
    )

    mft_bytes = b"".join(records)
    mft_bytes = mft_bytes.ljust(length_clusters * _RECORDS_PER_CLUSTER * _RECORD_SIZE, b"\x00")
    return (b"\x00" * _MFT_BYTE_OFFSET) + mft_bytes


def _build_fake_volume_with_cross_subtree_hardlink():
    """Record 0: real $MFT record (one extent). 1-4: unused. 5: root.
    6: Sub (directory, under root). 7: the SAME real file hard-linked
    twice -- once directly under root as "in_root.bin", once under Sub as
    "in_sub.bin". This is the exact real-world pattern that caused the
    validation-gate-caught bug (a system file hard-linked between
    C:\\Windows\\Fonts and C:\\Windows\\WinSxS): a scan of just "C:\\Sub"
    must bill "in_sub.bin" in full, never zeroed just because its OTHER
    occurrence lies outside the requested subtree."""
    root_frn = _pack_frn(1, 5)
    sub_frn = _pack_frn(1, 6)

    total_records = 8  # 0..7
    length_clusters = -(-total_records // _RECORDS_PER_CLUSTER)  # ceil division
    record0 = _build_mft_record0([(length_clusters, _MFT_START_LCN)])

    records = [record0] + [_unused_record(n) for n in range(1, 5)]
    records.append(
        _build_record(
            5, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, ".")
        )
    )
    records.append(
        _build_record(
            6, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, "Sub")
        )
    )
    records.append(
        _build_record(
            7,
            is_directory=False,
            sequence_number=1,
            file_names=[
                _file_name_value(root_frn, "in_root.bin"),
                _file_name_value(sub_frn, "in_sub.bin"),
            ],
            data=b"hello world",
        )
    )

    mft_bytes = b"".join(records)
    mft_bytes = mft_bytes.ljust(length_clusters * _RECORDS_PER_CLUSTER * _RECORD_SIZE, b"\x00")
    return (b"\x00" * _MFT_BYTE_OFFSET) + mft_bytes


_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # a junction, or a volume mount point
_IO_REPARSE_TAG_CLOUD = 0x9000001A  # OneDrive Files On-Demand


def _build_fake_volume_with_a_junction_and_onedrive():
    """Record 0: real $MFT record (one extent). 1-4: unused. 5: root.
    6: "Link", a junction -- its own directory index is empty on real NTFS
    (a non-empty folder can't become one), so nothing lives under it. 7:
    "OneDrive", the attributes measured on a real OneDrive folder (0x431,
    reparse bit included, cloud tag). 8: "synced.txt" under OneDrive."""
    root_frn = _pack_frn(1, 5)
    onedrive_frn = _pack_frn(1, 7)

    total_records = 9  # 0..8
    length_clusters = -(-total_records // _RECORDS_PER_CLUSTER)  # ceil division
    record0 = _build_mft_record0([(length_clusters, _MFT_START_LCN)])

    records = [record0] + [_unused_record(n) for n in range(1, 5)]
    records.append(
        _build_record(
            5, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, ".")
        )
    )
    records.append(
        _build_record(
            6,
            is_directory=True,
            sequence_number=1,
            file_attributes=0x10,
            reparse_tag=_IO_REPARSE_TAG_MOUNT_POINT,
            file_name=_file_name_value(root_frn, "Link"),
        )
    )
    records.append(
        _build_record(
            7,
            is_directory=True,
            sequence_number=1,
            file_attributes=0x31,
            reparse_tag=_IO_REPARSE_TAG_CLOUD,
            file_name=_file_name_value(root_frn, "OneDrive"),
        )
    )
    records.append(
        _build_record(
            8,
            is_directory=False,
            sequence_number=1,
            file_name=_file_name_value(onedrive_frn, "synced.txt"),
            data=b"synced!",
        )
    )

    mft_bytes = b"".join(records)
    mft_bytes = mft_bytes.ljust(length_clusters * _RECORDS_PER_CLUSTER * _RECORD_SIZE, b"\x00")
    return (b"\x00" * _MFT_BYTE_OFFSET) + mft_bytes


class _FakeCreateFileW:
    """A plain callable object, not a bound method -- mft_volume.py sets
    `.restype` on this attribute, which a bound method can't accept."""

    def __init__(self):
        self.restype = None

    def __call__(self, path, access, share, security, disposition, flags, template):
        return 424242


class _FakeKernel32:
    """Also fakes a minimal, working USN Change Journal (FSCTL_QUERY_USN_
    JOURNAL/FSCTL_CREATE_USN_JOURNAL/FSCTL_READ_USN_JOURNAL) alongside the
    NTFS volume-data FSCTL turbo_read.scan_subtree_using_cache's caching
    path now also exercises on every real scan -- a first attempt at this
    fake only ever handled one FSCTL code regardless of what was actually
    requested, which would have silently fed USN journal calls garbage
    _NTFS_VOLUME_DATA_BUFFER-shaped data instead of failing loudly."""

    def __init__(self, volume_bytes, volume_serial=1, usn_journal_id=777):
        self.volume_bytes = volume_bytes
        self.volume_serial = volume_serial
        self._file_pointer = 0
        self.CreateFileW = _FakeCreateFileW()
        self.usn_journal_id = usn_journal_id
        self.usn_first_usn = 0
        self.usn_next_usn = 1000
        self.usn_lowest_valid_usn = 0
        # Queued raw FSCTL_READ_USN_JOURNAL response payloads (8-byte next-
        # USN cursor + zero or more packed USN_RECORDs) -- tests inject
        # simulated file changes here; defaults to "caught up, no records".
        self.usn_read_responses = []
        self.read_file_calls = 0

    def GetDriveTypeW(self, root_path):
        return drive_info._DRIVE_FIXED

    def GetVolumeInformationW(self, root_path, _n1, _n2, _n3, _n4, _n5, fs_name_buffer, _n6):
        fs_name_buffer.value = "NTFS"
        return 1

    def DeviceIoControl(
        self, handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped
    ):
        if code == mft_volume._FSCTL_GET_NTFS_VOLUME_DATA:
            info = ctypes.cast(
                out_ref, ctypes.POINTER(mft_volume._NTFS_VOLUME_DATA_BUFFER)
            ).contents
            info.VolumeSerialNumber = self.volume_serial
            info.BytesPerSector = _SECTOR_SIZE
            info.BytesPerCluster = _BYTES_PER_CLUSTER
            info.BytesPerFileRecordSegment = _RECORD_SIZE
            info.MftValidDataLength = 0  # unused -- record_count now comes from decoded extents
            info.MftStartLcn = _MFT_START_LCN
            return 1

        if code == usn_journal._FSCTL_QUERY_USN_JOURNAL:
            info = ctypes.cast(out_ref, ctypes.POINTER(usn_journal._USN_JOURNAL_DATA_V0)).contents
            info.UsnJournalID = self.usn_journal_id
            info.FirstUsn = self.usn_first_usn
            info.NextUsn = self.usn_next_usn
            info.LowestValidUsn = self.usn_lowest_valid_usn
            info.MaxUsn = 999_999_999
            info.MaximumSize = 0
            info.AllocationDelta = 0
            return 1

        if code == usn_journal._FSCTL_CREATE_USN_JOURNAL:
            return 1

        if code == usn_journal._FSCTL_READ_USN_JOURNAL:
            request = ctypes.cast(
                in_buf, ctypes.POINTER(usn_journal._READ_USN_JOURNAL_DATA_V0)
            ).contents
            if request.UsnJournalID != self.usn_journal_id:
                return 0
            payload = (
                self.usn_read_responses.pop(0)
                if self.usn_read_responses
                else struct.pack("<q", self.usn_next_usn)  # caught up: no records
            )
            out_ref.raw = payload.ljust(out_size, b"\x00")
            ctypes.cast(bytes_ret_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = len(payload)
            return 1

        raise AssertionError(f"unexpected FSCTL code {code:#x}")

    def SetFilePointerEx(self, handle, distance, new_position_ref, method):
        self._file_pointer = distance.value
        return 1

    def ReadFile(self, handle, buffer, length, bytes_read_ref, overlapped):
        self.read_file_calls += 1
        data = self.volume_bytes[self._file_pointer : self._file_pointer + length]
        buffer.raw = data.ljust(length, b"\x00")
        ctypes.cast(bytes_read_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = length
        return 1

    def CloseHandle(self, handle):
        return 1


class _FakeWinDLL:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def _init_cache_db(tmp_path, monkeypatch):
    monkeypatch.setattr(turbo_cache, "DB_NAME", tmp_path / "turbo_scan_cache.db")
    turbo_cache.init_cache_db()


def test_full_pipeline_scans_a_subfolder_through_the_real_engine(monkeypatch, tmp_path):
    _init_cache_db(tmp_path, monkeypatch)
    volume_bytes = _build_fake_volume()
    kernel32 = _FakeKernel32(volume_bytes)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Sub",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert report.engine == turbo_scan.ENGINE_TURBO
    assert report.fallback_reason is None
    assert node.name == "Sub"
    assert node.is_dir
    assert node.file_count == 1
    assert node.size == 5  # len(b"xyz12")

    child_names = {c.name for c in node.children}
    assert child_names == {"inside.txt"}


def test_full_pipeline_scans_the_whole_volume(monkeypatch, tmp_path):
    _init_cache_db(tmp_path, monkeypatch)
    volume_bytes = _build_fake_volume()
    kernel32 = _FakeKernel32(volume_bytes)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert report.engine == turbo_scan.ENGINE_TURBO
    assert node.file_count == 2  # hello.txt + Sub/inside.txt
    assert node.size == 3 + 5  # "hi!" + "xyz12"

    child_names = {c.name for c in node.children}
    assert child_names == {"hello.txt", "Sub"}


def test_hardlink_with_one_occurrence_outside_the_scanned_subtree_is_billed_in_full(
    monkeypatch, tmp_path
):
    # The exact real-world bug the validation gate caught: without the
    # fix, scanning just "C:\Sub" would zero out "in_sub.bin" because the
    # SAME file's other occurrence ("in_root.bin", outside this subtree
    # entirely) got picked as the whole volume's "primary" instead.
    _init_cache_db(tmp_path, monkeypatch)
    volume_bytes = _build_fake_volume_with_cross_subtree_hardlink()
    kernel32 = _FakeKernel32(volume_bytes)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Sub",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert report.engine == turbo_scan.ENGINE_TURBO
    assert node.name == "Sub"

    child_names = {c.name for c in node.children}
    assert child_names == {"in_sub.bin"}

    in_sub = node.children[0]
    assert in_sub.hardlink_dup is False
    assert in_sub.size == len(b"hello world")
    assert node.size == len(b"hello world")
    assert node.file_count == 1


def _use_fake_volume(monkeypatch, tmp_path, kernel32):
    _init_cache_db(tmp_path, monkeypatch)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)


def _scan(path):
    return turbo_scan.scan_with_best_engine(
        path, queue.Queue(), threading.Event(), turbo_enabled=True
    )


def test_a_onedrive_folder_is_scanned_like_any_folder(monkeypatch, tmp_path):
    # OneDrive folders carry the reparse bit in NTFS's own attributes; Turbo
    # Scan once showed them as 0-byte links in a scan it called complete.
    volume_bytes = _build_fake_volume_with_a_junction_and_onedrive()
    _use_fake_volume(monkeypatch, tmp_path, _FakeKernel32(volume_bytes))

    volume, full_report = _scan("C:\\")
    folder, cached_report = _scan("C:\\OneDrive")  # the same folder, from the cache

    assert full_report.engine == cached_report.engine == turbo_scan.ENGINE_TURBO
    assert cached_report.mft_read == MftRead(incremental=True)
    children = {c.name: c for c in volume.children}
    assert children["Link"].is_link  # a junction met during a scan stays a leaf
    onedrive = children["OneDrive"]
    assert onedrive.is_dir and not onedrive.is_link
    for node in (onedrive, folder):
        assert (node.size, node.file_count) == (len(b"synced!"), 1)
        assert {c.name for c in node.children} == {"synced.txt"}


@pytest.mark.parametrize("cached", [False, True])
def test_a_junction_asked_for_directly_falls_back_to_compatible(monkeypatch, tmp_path, cached):
    # A junction's own directory index is always empty, so Turbo Scan
    # showed 0 bytes in 0 files where Compatible found the target's files.
    # Both reading paths -- a full MFT read and the cache -- must refuse.
    volume_bytes = _build_fake_volume_with_a_junction_and_onedrive()
    _use_fake_volume(monkeypatch, tmp_path, _FakeKernel32(volume_bytes))
    if cached:
        _scan("C:\\")
    compatible_result = Node("C:\\Link", "Link")
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: compatible_result)

    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Link", queue.Queue(), threading.Event(), turbo_enabled=True
    )

    assert node is compatible_result
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE
    assert "junction" in report.fallback_reason


# -- cache + USN Journal incremental refresh --------------------------------- #
# The single most valuable coverage for this feature: catching wiring
# mistakes *between* turbo_cache/usn_journal/turbo_scan that per-module unit
# tests (test_turbo_cache.py, test_usn_journal.py) can't see, by driving the
# real, unmocked orchestration through two scans of the same fake volume.


def _pack_usn_record(frn, parent_frn, usn, reason):
    return struct.pack(
        usn_journal._USN_RECORD_HEADER_FORMAT,
        usn_journal._USN_RECORD_HEADER_SIZE,
        2,
        0,
        frn,
        parent_frn,
        usn,
        0,
        reason,
        0,
        0,
        0,
        0,
        usn_journal._USN_RECORD_HEADER_SIZE,
    )


def _usn_read_response(next_usn, records_bytes=b""):
    return struct.pack("<q", next_usn) + records_bytes


def test_second_scan_of_an_unchanged_volume_uses_incremental_refresh(monkeypatch, tmp_path):
    _init_cache_db(tmp_path, monkeypatch)
    volume_bytes = _build_fake_volume()
    kernel32 = _FakeKernel32(volume_bytes)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    first_node, first_report = turbo_scan.scan_with_best_engine(
        "C:\\",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )
    assert first_report.engine == turbo_scan.ENGINE_TURBO
    assert first_report.mft_read == MftRead(
        incremental=False, full_read_reason="first scan of this drive"
    )
    reads_for_full_scan = kernel32.read_file_calls
    assert reads_for_full_scan > 0

    kernel32.read_file_calls = 0
    second_node, second_report = turbo_scan.scan_with_best_engine(
        "C:\\",
        queue.Queue(),
        threading.Event(),
        turbo_enabled=True,
    )

    assert second_report.engine == turbo_scan.ENGINE_TURBO
    assert second_report.mft_read == MftRead(incremental=True)
    assert second_node.file_count == first_node.file_count == 2
    assert second_node.size == first_node.size == 3 + 5
    assert {c.name for c in second_node.children} == {"hello.txt", "Sub"}
    # The incremental path never re-walks the whole MFT sequentially --
    # only record #0 (re-bootstrapping $MFT's own extent layout) plus a
    # handful of chunk reads for whatever the (empty) USN journal query
    # touches, nowhere near a full 9-record walk's worth of chunk loads.
    assert kernel32.read_file_calls < reads_for_full_scan


def test_second_scan_picks_up_a_new_file_via_the_journal_without_a_full_reread(
    monkeypatch, tmp_path
):
    _init_cache_db(tmp_path, monkeypatch)
    volume_bytes = bytearray(_build_fake_volume())
    kernel32 = _FakeKernel32(bytes(volume_bytes))
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    first_node, first_report = turbo_scan.scan_with_best_engine(
        "C:\\",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )
    assert first_report.engine == turbo_scan.ENGINE_TURBO
    assert {c.name for c in first_node.children} == {"hello.txt", "Sub"}

    # Record 9 is already-allocated-but-unused space in the fake volume's
    # single MFT extent (_build_fake_volume rounds its extent up to a
    # whole number of clusters) -- write a real new file there without
    # touching $MFT's own extent layout/record_count at all, exactly like
    # NTFS reusing free MFT slots for a newly created file in real life.
    root_frn = _pack_frn(1, 5)
    new_record = _build_record(
        9,
        is_directory=False,
        sequence_number=1,
        file_name=_file_name_value(root_frn, "new.txt"),
        data=b"NEW",
    )
    new_record_offset = _MFT_BYTE_OFFSET + 9 * _RECORD_SIZE
    volume_bytes[new_record_offset : new_record_offset + _RECORD_SIZE] = new_record
    kernel32.volume_bytes = bytes(volume_bytes)

    new_file_frn = _pack_frn(1, 9)
    usn_record = _pack_usn_record(new_file_frn, root_frn, usn=1050, reason=0x100)  # FILE_CREATE
    kernel32.usn_read_responses = [
        _usn_read_response(next_usn=1100, records_bytes=usn_record),
        _usn_read_response(next_usn=1100),  # caught up
    ]

    second_node, second_report = turbo_scan.scan_with_best_engine(
        "C:\\",
        queue.Queue(),
        threading.Event(),
        turbo_enabled=True,
    )

    assert second_report.engine == turbo_scan.ENGINE_TURBO
    assert second_report.mft_read == MftRead(incremental=True)
    assert {c.name for c in second_node.children} == {"hello.txt", "Sub", "new.txt"}
    assert second_node.file_count == 3  # hello.txt + Sub/inside.txt + new.txt
    assert second_node.size == 3 + 5 + 3  # "hi!" + "xyz12" + "NEW"


def test_rescanning_one_folder_loads_it_from_the_cache_matching_a_full_read(monkeypatch, tmp_path):
    _init_cache_db(tmp_path, monkeypatch)
    kernel32 = _FakeKernel32(_build_fake_volume())
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    def scan(path):
        return turbo_scan.scan_with_best_engine(
            path, queue.Queue(), threading.Event(), turbo_enabled=True
        )

    full_node, full_report = scan("C:\\Sub")  # first scan: nothing cached yet
    cached_node, cached_report = scan("C:\\sub")  # folder typed in a different case

    assert full_report.mft_read.incremental is False
    assert cached_report.mft_read == MftRead(incremental=True)
    assert (cached_node.path, cached_node.name) == ("C:\\Sub", "Sub")  # disk spelling
    for node in (full_node, cached_node):
        assert {c.name for c in node.children} == {"inside.txt"}
        assert (node.size, node.file_count) == (5, 1)


class _JournalledKernel32(_FakeKernel32):
    """A USN journal that answers FSCTL_READ_USN_JOURNAL from its StartUsn,
    as the real one does, plus one file change made while a full read is
    under way: right after the chunk holding `change_record` has been read,
    that record's bytes are replaced and the change is journalled."""

    def __init__(self, volume_bytes, change_record, new_record_bytes, parent_frn):
        super().__init__(volume_bytes)
        self.entries = []  # (usn, frn, parent_frn, reason)
        self._change = (change_record, new_record_bytes, parent_frn)

    def ReadFile(self, handle, buffer, length, bytes_read_ref, overlapped):
        start = self._file_pointer
        result = super().ReadFile(handle, buffer, length, bytes_read_ref, overlapped)
        if self._change is not None:
            record_number, new_bytes, parent_frn = self._change
            offset = _MFT_BYTE_OFFSET + record_number * _RECORD_SIZE
            if start <= offset < start + length:
                volume = bytearray(self.volume_bytes)
                volume[offset : offset + _RECORD_SIZE] = new_bytes
                self.volume_bytes = bytes(volume)
                frn = _pack_frn(1, record_number)
                self.entries.append((self.usn_next_usn, frn, parent_frn, 0x80000002))
                self.usn_next_usn += 100  # DATA_EXTEND | CLOSE, then the head moves on
                self._change = None
        return result

    def DeviceIoControl(
        self, handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped
    ):
        if code != usn_journal._FSCTL_READ_USN_JOURNAL:
            return super().DeviceIoControl(
                handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped
            )
        request = ctypes.cast(in_buf, ctypes.POINTER(usn_journal._READ_USN_JOURNAL_DATA_V0))
        start_usn = request.contents.StartUsn
        records = b"".join(
            _pack_usn_record(frn, parent, usn, reason)
            for usn, frn, parent, reason in self.entries
            if usn >= start_usn
        )
        payload = _usn_read_response(self.usn_next_usn, records)
        out_ref.raw = payload.ljust(out_size, b"\x00")
        ctypes.cast(bytes_ret_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = len(payload)
        return 1


def test_a_change_made_during_a_full_read_reaches_the_next_incremental_scan(monkeypatch, tmp_path):
    # The journal cursor used to be taken after the full read and the cache
    # write, so a change to a record already read fell before it and was
    # never applied: the full scan and every incremental one after it
    # showed the old size.
    sub_frn = _pack_frn(1, 7)
    grown = _build_record(
        8,
        is_directory=False,
        sequence_number=1,
        file_name=_file_name_value(sub_frn, "inside.txt"),
        data=b"g" * 600,
    )
    kernel32 = _JournalledKernel32(_build_fake_volume(), 8, grown, sub_frn)
    _use_fake_volume(monkeypatch, tmp_path, kernel32)

    during, during_report = _scan("C:\\Sub")
    after, after_report = _scan("C:\\Sub")

    assert during_report.mft_read.incremental is False
    assert during.size == len(b"xyz12")  # read before the change was made
    assert kernel32.entries  # the change did happen mid-read
    assert after_report.mft_read == MftRead(incremental=True)
    assert after.size == 600
