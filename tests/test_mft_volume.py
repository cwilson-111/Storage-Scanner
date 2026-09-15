"""Tests for storage_scanner.mft_volume.RecordSource against a faked
ctypes.windll.kernel32 -- the same fake-the-Win32-call technique already
used by tests/test_elevation.py and tests/test_drive_info.py.

The real CreateFileW/DeviceIoControl/ReadFile calls can only be verified
end-to-end on a real, elevated Windows session against a real NTFS volume
(see the Turbo Scan plan). What's tested here is RecordSource's own logic
around them: chunk caching (so sequential access doesn't issue a syscall
per record), offset arithmetic, bounds checking, and that every failure
path raises MftVolumeError rather than some other exception or a silent
wrong answer.
"""

import ctypes
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import mft_volume
from storage_scanner.mft_volume import MftVolumeError, RecordSource

_FAKE_HANDLE = 12345
_BYTES_PER_CLUSTER = 4096
_RECORD_SIZE = 1024


class _FakeCreateFileW:
    def __init__(self, fails=False):
        self.fails = fails
        self.restype = None
        self.calls = []

    def __call__(self, path, access, share, security, disposition, flags, template):
        self.calls.append(path)
        if self.fails:
            return mft_volume._INVALID_HANDLE_VALUE
        return _FAKE_HANDLE


class _FakeDeviceIoControl:
    def __init__(self, mft_byte_offset, valid_data_length, fails=False, record_size=_RECORD_SIZE):
        self.mft_byte_offset = mft_byte_offset
        self.valid_data_length = valid_data_length
        self.fails = fails
        self.record_size = record_size
        self.calls = 0

    def __call__(self, handle, code, in_buf, in_size, out_ref, out_size, bytes_ret_ref, overlapped):
        self.calls += 1
        if self.fails:
            return 0
        info = ctypes.cast(out_ref, ctypes.POINTER(mft_volume._NTFS_VOLUME_DATA_BUFFER)).contents
        info.BytesPerSector = 512
        info.BytesPerCluster = _BYTES_PER_CLUSTER
        info.BytesPerFileRecordSegment = self.record_size
        info.MftValidDataLength = self.valid_data_length
        info.MftStartLcn = self.mft_byte_offset // _BYTES_PER_CLUSTER
        return 1


class _FakeSetFilePointerEx:
    def __init__(self):
        self.last_offset = None
        self.calls = 0

    def __call__(self, handle, distance, new_position_ref, method):
        self.calls += 1
        self.last_offset = distance.value
        return 1


class _FakeReadFile:
    """Simulates reading from a virtual disk image (`volume_bytes`) at
    whatever offset the paired _FakeSetFilePointerEx last recorded."""

    def __init__(self, volume_bytes, pointer, fails=False, short_read=False):
        self.volume_bytes = volume_bytes
        self.pointer = pointer  # the _FakeSetFilePointerEx to read current offset from
        self.fails = fails
        self.short_read = short_read
        self.calls = []  # (offset, length)

    def __call__(self, handle, buffer, length, bytes_read_ref, overlapped):
        if self.fails:
            return 0
        offset = self.pointer.last_offset
        self.calls.append((offset, length))
        available = self.volume_bytes[offset:offset + length]
        # A "short read" should look wrong even when the underlying fake
        # volume happens to have plenty of bytes available -- it's meant
        # to simulate a genuine I/O hiccup, not running off the end of data.
        actual_length = length - 1 if self.short_read else length
        data = available[:actual_length].ljust(length, b"\x00")
        buffer.raw = data
        ctypes.cast(bytes_read_ref, ctypes.POINTER(ctypes.c_uint32)).contents.value = actual_length
        return 1


class _FakeCloseHandle:
    def __init__(self):
        self.calls = 0

    def __call__(self, handle):
        self.calls += 1
        return 1


class _FakeKernel32:
    def __init__(
        self, volume_bytes, mft_byte_offset, valid_data_length=None,
        record_size=_RECORD_SIZE, create_file_fails=False,
        device_io_control_fails=False, read_file_fails=False, short_read=False,
    ):
        if valid_data_length is None:
            valid_data_length = len(volume_bytes) - mft_byte_offset
        self.CreateFileW = _FakeCreateFileW(fails=create_file_fails)
        self.DeviceIoControl = _FakeDeviceIoControl(
            mft_byte_offset, valid_data_length, fails=device_io_control_fails,
            record_size=record_size,
        )
        self.SetFilePointerEx = _FakeSetFilePointerEx()
        self.ReadFile = _FakeReadFile(
            volume_bytes, self.SetFilePointerEx, fails=read_file_fails, short_read=short_read,
        )
        self.CloseHandle = _FakeCloseHandle()


class _FakeWinDLL:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def _make_volume(record_count, mft_byte_offset=8192, record_size=_RECORD_SIZE):
    """A fake volume: `mft_byte_offset` bytes of filler, then `record_count`
    fixed-size records, each filled with a byte equal to its own record
    number (mod 256) so reads can be checked for correct content, not just
    correct length."""
    filler = b"\x00" * mft_byte_offset
    records = b"".join(bytes([i % 256]) * record_size for i in range(record_count))
    return filler + records


def _patch(monkeypatch, kernel32):
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)


def test_opens_volume_and_reports_record_count(monkeypatch):
    volume_bytes = _make_volume(record_count=10)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")

    assert source.record_count == 10
    assert kernel32.CreateFileW.calls == ["\\\\.\\C:"]


def test_sequential_reads_within_one_chunk_issue_a_single_syscall(monkeypatch):
    volume_bytes = _make_volume(record_count=10)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    source.record_at(0)
    source.record_at(1)
    source.record_at(9)

    assert len(kernel32.ReadFile.calls) == 1


def test_record_at_returns_the_right_record_content(monkeypatch):
    volume_bytes = _make_volume(record_count=5)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    record = source.record_at(3)

    assert len(record) == _RECORD_SIZE
    assert record == bytes([3]) * _RECORD_SIZE


def test_reading_a_record_in_a_different_chunk_issues_a_new_read(monkeypatch):
    # Force a tiny chunk size (2 records per chunk) by using a huge
    # per-record size relative to the read-chunk constant.
    monkeypatch.setattr(mft_volume, "_READ_CHUNK_BYTES", 2 * _RECORD_SIZE)
    volume_bytes = _make_volume(record_count=6)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    source.record_at(0)  # loads chunk [0, 1]
    source.record_at(1)  # served from cache
    assert len(kernel32.ReadFile.calls) == 1

    record = source.record_at(4)  # outside the first chunk -- new read
    assert len(kernel32.ReadFile.calls) == 2
    assert record == bytes([4]) * _RECORD_SIZE


def test_record_at_out_of_range_raises_index_error(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    with pytest.raises(IndexError):
        source.record_at(3)
    with pytest.raises(IndexError):
        source.record_at(-1)


def test_create_file_failure_raises_mft_volume_error(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192, create_file_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")


def test_device_io_control_failure_raises_and_closes_the_handle(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192, device_io_control_fails=True)
    _patch(monkeypatch, kernel32)

    with pytest.raises(MftVolumeError):
        RecordSource("C:\\")

    assert kernel32.CloseHandle.calls == 1


def test_read_file_failure_raises_mft_volume_error(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192, read_file_fails=True)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    with pytest.raises(MftVolumeError):
        source.record_at(0)


def test_short_read_raises_mft_volume_error_rather_than_silently_truncating(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192, short_read=True)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    with pytest.raises(MftVolumeError):
        source.record_at(0)


def test_close_calls_close_handle(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    source = RecordSource("C:\\")
    source.close()

    assert kernel32.CloseHandle.calls == 1


def test_context_manager_closes_on_exit(monkeypatch):
    volume_bytes = _make_volume(record_count=3)
    kernel32 = _FakeKernel32(volume_bytes, mft_byte_offset=8192)
    _patch(monkeypatch, kernel32)

    with RecordSource("C:\\") as source:
        source.record_at(0)

    assert kernel32.CloseHandle.calls == 1
