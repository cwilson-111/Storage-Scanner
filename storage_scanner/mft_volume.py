"""Raw NTFS volume access for Turbo Scan: opens a volume, locates its
Master File Table via FSCTL_GET_NTFS_VOLUME_DATA, and streams its raw
bytes in large chunks, one MFT record at a time.

Deliberately thin on branching logic -- all real parsing/tree-building
logic lives in mft_parser.py/mft_scan.py instead, which take RecordSource
as a duck-typed dependency (`record_at(n) -> bytes`, `record_count`) and
carry the bulk of this feature's test coverage with hand-built fakes. The
actual CreateFileW/DeviceIoControl/ReadFile calls here can only be
exercised end-to-end on a real, elevated Windows session against a real
NTFS volume -- but the chunk-caching and offset arithmetic around them
(RecordSource's real logic) is unit-tested in tests/test_mft_volume.py
against a faked ctypes.windll.kernel32, the same technique already used
for storage_scanner.drive_info/file_ops.
"""

import ctypes
from ctypes import wintypes

from storage_scanner.logging_setup import logger

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_BEGIN = 0

_FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064

# A multiple of both the sector size and any real BytesPerFileRecordSegment
# (1024 on every NTFS volume in practice) -- large enough to make sequential
# scans through the whole MFT fast, without needing a syscall per record.
_READ_CHUNK_BYTES = 4 * 1024 * 1024


class MftVolumeError(Exception):
    """Any failure opening a volume or reading its $MFT. Always caught by
    storage_scanner.turbo_scan's broad fallback-to-Compatible-engine
    handling -- never expected to reach a user directly."""


class _NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_int64),
        ("NumberSectors", ctypes.c_int64),
        ("TotalClusters", ctypes.c_int64),
        ("FreeClusters", ctypes.c_int64),
        ("TotalReserved", ctypes.c_int64),
        ("BytesPerSector", wintypes.DWORD),
        ("BytesPerCluster", wintypes.DWORD),
        ("BytesPerFileRecordSegment", wintypes.DWORD),
        ("ClustersPerFileRecordSegment", wintypes.DWORD),
        ("MftValidDataLength", ctypes.c_int64),
        ("MftStartLcn", ctypes.c_int64),
        ("Mft2StartLcn", ctypes.c_int64),
        ("MftZoneStart", ctypes.c_int64),
        ("MftZoneEnd", ctypes.c_int64),
    ]


def _open_volume(volume_root):
    # CreateFileW wants the bare device path ("\\\\.\\C:", no trailing
    # backslash) for raw volume access -- volume_root (from
    # drive_info.get_volume_root) is "C:\\", so strip the separator.
    device_path = "\\\\.\\" + volume_root.rstrip("\\")
    kernel32 = ctypes.windll.kernel32
    # HANDLE is pointer-sized -- without this, ctypes' default 32-bit
    # signed-int return type would truncate/misinterpret it on 64-bit
    # Windows (the same class of bug file_ops.py's ShellExecuteW binding
    # already works around for its own pointer-sized HINSTANCE return).
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        device_path, _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, 0, None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise MftVolumeError(
            f"Could not open {device_path!r} for raw access (admin rights required)"
        )
    return handle


def _get_ntfs_volume_data(handle):
    buffer = _NTFS_VOLUME_DATA_BUFFER()
    bytes_returned = wintypes.DWORD(0)
    succeeded = ctypes.windll.kernel32.DeviceIoControl(
        handle, _FSCTL_GET_NTFS_VOLUME_DATA, None, 0,
        ctypes.byref(buffer), ctypes.sizeof(buffer),
        ctypes.byref(bytes_returned), None,
    )
    if not succeeded:
        raise MftVolumeError("FSCTL_GET_NTFS_VOLUME_DATA failed")
    return buffer


def _set_file_pointer(handle, offset):
    # SetFilePointerEx takes a signed 64-bit distance -- ctypes needs the
    # exact width declared or the high 32 bits silently get dropped.
    distance = ctypes.c_int64(offset)
    new_position = ctypes.c_int64(0)
    succeeded = ctypes.windll.kernel32.SetFilePointerEx(
        handle, distance, ctypes.byref(new_position), _FILE_BEGIN,
    )
    if not succeeded:
        raise MftVolumeError(f"SetFilePointerEx failed seeking to offset {offset}")


def _read_bytes(handle, offset, length):
    _set_file_pointer(handle, offset)
    buffer = ctypes.create_string_buffer(length)
    bytes_read = wintypes.DWORD(0)
    succeeded = ctypes.windll.kernel32.ReadFile(
        handle, buffer, length, ctypes.byref(bytes_read), None,
    )
    if not succeeded:
        raise MftVolumeError(f"ReadFile failed at offset {offset} (length {length})")
    if bytes_read.value != length:
        raise MftVolumeError(
            f"ReadFile returned {bytes_read.value} bytes, expected {length}, "
            f"at offset {offset}"
        )
    return buffer.raw[:bytes_read.value]


def _close_handle(handle):
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except OSError:
        logger.debug("CloseHandle failed for an MFT volume handle", exc_info=True)


class RecordSource:
    """Duck-typed record source for mft_parser.parse_base_record and
    mft_scan.build_tree: `record_at(n) -> bytes` plus a `record_count`
    attribute, backed by a raw NTFS volume handle.

    Sequential access (the common case -- walking record 0, 1, 2, ...) is
    served from a large in-memory chunk with no syscall per record.
    Out-of-order access (mft_parser's occasional $ATTRIBUTE_LIST lookups
    into an earlier record) still works correctly, just via a fresh chunk
    load -- correctness first, since that path is rare.
    """

    def __init__(self, volume_root):
        self._handle = _open_volume(volume_root)
        try:
            volume_data = _get_ntfs_volume_data(self._handle)
        except MftVolumeError:
            _close_handle(self._handle)
            raise
        self._record_size = volume_data.BytesPerFileRecordSegment
        self._mft_byte_offset = volume_data.MftStartLcn * volume_data.BytesPerCluster
        if self._record_size <= 0:
            _close_handle(self._handle)
            raise MftVolumeError(
                f"Implausible BytesPerFileRecordSegment ({self._record_size}) reported "
                f"for {volume_root!r}"
            )
        self.record_count = volume_data.MftValidDataLength // self._record_size
        self._chunk_records = max(1, _READ_CHUNK_BYTES // self._record_size)
        self._chunk_start_record = None
        self._chunk_data = b""

    def record_at(self, record_number):
        if not 0 <= record_number < self.record_count:
            raise IndexError(record_number)
        chunk_start = (record_number // self._chunk_records) * self._chunk_records
        if chunk_start != self._chunk_start_record:
            self._load_chunk(chunk_start)
        local_offset = (record_number - chunk_start) * self._record_size
        return self._chunk_data[local_offset:local_offset + self._record_size]

    def _load_chunk(self, chunk_start_record):
        records_to_read = min(self._chunk_records, self.record_count - chunk_start_record)
        length = records_to_read * self._record_size
        offset = self._mft_byte_offset + chunk_start_record * self._record_size
        self._chunk_data = _read_bytes(self._handle, offset, length)
        self._chunk_start_record = chunk_start_record

    def close(self):
        _close_handle(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def open_record_source(volume_root):
    return RecordSource(volume_root)
