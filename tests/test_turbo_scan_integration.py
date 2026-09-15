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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import drive_info, mft_volume, turbo_scan
from storage_scanner.mft_parser import _pack_frn

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


def _std_info_value():
    return struct.pack("<QQQQI", 0, 0, 0, 0, 0)


def _file_name_value(parent_frn, name):
    fixed = struct.pack("<QQQQQQQII", parent_frn, 0, 0, 0, 0, 0, 0, 0, 0)
    name_bytes = name.encode("utf-16-le")
    return fixed + bytes([len(name), 1]) + name_bytes  # namespace 1 = Win32


def _stamp_fixups(record):
    record = bytearray(record)
    usn = b"\x01\x00"
    record[_USA_OFFSET:_USA_OFFSET + 2] = usn
    for i in range(1, _USA_SIZE):
        sector_end = i * _SECTOR_SIZE - 2
        original = bytes(record[sector_end:sector_end + 2])
        record[_USA_OFFSET + 2 * i:_USA_OFFSET + 2 * i + 2] = original
        record[sector_end:sector_end + 2] = usn
    return bytes(record)


def _build_record(record_number, *, is_directory, sequence_number, file_name, data=None):
    attrs = bytearray()
    attrs += _resident_attr(_ATTR_STANDARD_INFORMATION, _std_info_value(), 0)
    attrs += _resident_attr(_ATTR_FILE_NAME, file_name, 1)
    if data is not None:
        attrs += _resident_attr(_ATTR_DATA, data, 2)
    attrs += struct.pack("<I", _ATTR_END_MARKER)

    flags = _RECORD_FLAG_IN_USE | (_RECORD_FLAG_IS_DIRECTORY if is_directory else 0)
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE", _USA_OFFSET, _USA_SIZE, 0, sequence_number, 1,
        _FIRST_ATTR_OFFSET, flags, _FIRST_ATTR_OFFSET + len(attrs), _RECORD_SIZE,
        0, 0, 0, record_number,
    )
    buf = bytearray(_RECORD_SIZE)
    buf[0:len(header)] = header
    buf[_FIRST_ATTR_OFFSET:_FIRST_ATTR_OFFSET + len(attrs)] = attrs
    return _stamp_fixups(bytes(buf))


def _unused_record(record_number):
    header = struct.pack(
        "<4sHHQHHHHIIQHHI",
        b"FILE", _USA_OFFSET, _USA_SIZE, 0, 1, 0,
        _FIRST_ATTR_OFFSET, 0, _FIRST_ATTR_OFFSET, _RECORD_SIZE,  # flags=0: not in use
        0, 0, 0, record_number,
    )
    buf = bytearray(_RECORD_SIZE)
    buf[0:len(header)] = header
    return _stamp_fixups(bytes(buf))


def _build_fake_volume():
    """Records 0-4: unused placeholders. 5: root. 6: hello.txt (under
    root). 7: Sub (directory, under root). 8: inside.txt (under Sub)."""
    root_frn = _pack_frn(1, 5)
    sub_frn = _pack_frn(1, 7)

    records = [_unused_record(n) for n in range(5)]
    records.append(_build_record(5, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, ".")))
    records.append(_build_record(6, is_directory=False, sequence_number=1, file_name=_file_name_value(root_frn, "hello.txt"), data=b"hi!"))
    records.append(_build_record(7, is_directory=True, sequence_number=1, file_name=_file_name_value(root_frn, "Sub")))
    records.append(_build_record(8, is_directory=False, sequence_number=1, file_name=_file_name_value(sub_frn, "inside.txt"), data=b"xyz12"))

    mft_bytes = b"".join(records)
    return (b"\x00" * _MFT_BYTE_OFFSET) + mft_bytes, len(records)


class _FakeCreateFileW:
    """A plain callable object, not a bound method -- mft_volume.py sets
    `.restype` on this attribute, which a bound method can't accept."""

    def __init__(self):
        self.restype = None

    def __call__(self, path, access, share, security, disposition, flags, template):
        return 424242


class _FakeKernel32:
    def __init__(self, volume_bytes, record_count):
        self.volume_bytes = volume_bytes
        self.record_count = record_count
        self._file_pointer = 0
        self.CreateFileW = _FakeCreateFileW()

    def GetDriveTypeW(self, root_path):
        return drive_info._DRIVE_FIXED

    def GetVolumeInformationW(self, root_path, _n1, _n2, _n3, _n4, _n5, fs_name_buffer, _n6):
        fs_name_buffer.value = "NTFS"
        return 1

    def DeviceIoControl(self, handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped):
        info = ctypes.cast(out_ref, ctypes.POINTER(mft_volume._NTFS_VOLUME_DATA_BUFFER)).contents
        info.BytesPerSector = _SECTOR_SIZE
        info.BytesPerCluster = _BYTES_PER_CLUSTER
        info.BytesPerFileRecordSegment = _RECORD_SIZE
        info.MftValidDataLength = self.record_count * _RECORD_SIZE
        info.MftStartLcn = _MFT_BYTE_OFFSET // _BYTES_PER_CLUSTER
        return 1

    def SetFilePointerEx(self, handle, distance, new_position_ref, method):
        self._file_pointer = distance.value
        return 1

    def ReadFile(self, handle, buffer, length, bytes_read_ref, overlapped):
        data = self.volume_bytes[self._file_pointer:self._file_pointer + length]
        buffer.raw = data.ljust(length, b"\x00")
        ctypes.cast(bytes_read_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = length
        return 1

    def CloseHandle(self, handle):
        return 1


class _FakeWinDLL:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def test_full_pipeline_scans_a_subfolder_through_the_real_engine(monkeypatch):
    volume_bytes, record_count = _build_fake_volume()
    kernel32 = _FakeKernel32(volume_bytes, record_count)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Sub", progress_q, cancel_event, turbo_enabled=True,
    )

    assert report.engine == turbo_scan.ENGINE_TURBO
    assert report.fallback_reason is None
    assert node.name == "Sub"
    assert node.is_dir
    assert node.file_count == 1
    assert node.size == 5  # len(b"xyz12")

    child_names = {c.name for c in node.children}
    assert child_names == {"inside.txt"}


def test_full_pipeline_scans_the_whole_volume(monkeypatch):
    volume_bytes, record_count = _build_fake_volume()
    kernel32 = _FakeKernel32(volume_bytes, record_count)
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)

    progress_q, cancel_event = queue.Queue(), threading.Event()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\", progress_q, cancel_event, turbo_enabled=True,
    )

    assert report.engine == turbo_scan.ENGINE_TURBO
    assert node.file_count == 2  # hello.txt + Sub/inside.txt
    assert node.size == 3 + 5    # "hi!" + "xyz12"

    child_names = {c.name for c in node.children}
    assert child_names == {"hello.txt", "Sub"}
