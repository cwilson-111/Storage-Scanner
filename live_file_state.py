"""What NTFS says about a file right now, for compare_scan_engines.classify:
how much it has allocated to it, and whether something has it open for
writing.

Both explain differences between the engines on a live drive (measured on a
real C:, 2026-10-06):

- NTFS reserves room past the end of a file that's being written: an open
  SQLite `models.db-wal` of 230,752 bytes had 327,680 allocated, which is
  what Turbo Scan (reading the MFT record) billed. The Compatible engine
  rounds the length up to a cluster: 233,472.
- A file open for writing has a moving size, and its MFT record on disk
  lags behind the size the file system hands out (an Edge `DIPS-wal`: 8,272
  bytes to the Compatible engine, 0 in its record), while its modified time
  can stay older than the run, so "changed during the run" misses it.
"""

import ctypes
import sys
from typing import NamedTuple, Optional

_FILE_READ_ATTRIBUTES = 0x80
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_STANDARD_INFO = 1  # FILE_INFO_BY_HANDLE_CLASS.FileStandardInfo
_ERROR_SHARING_VIOLATION = 32


class LiveState(NamedTuple):
    allocation: Optional[int]  # FileStandardInfo.AllocationSize, None if unreadable
    open_for_writing: bool  # someone holds it open for writing (or exclusively)


def _kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]  # fmt: skip
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32, wintypes.HANDLE(-1).value


def live_file_state(path):
    """`path`'s LiveState now. Off Windows, or when the file can't be
    opened at all, nothing is known: LiveState(None, False)."""
    if sys.platform != "win32":
        return LiveState(None, False)
    kernel32, invalid = _kernel32()

    allocation = None
    # Opening for attributes only never conflicts with another opener.
    handle = kernel32.CreateFileW(
        path,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle not in (None, invalid):
        info = (ctypes.c_byte * 24)()  # AllocationSize, EndOfFile, links, flags
        if kernel32.GetFileInformationByHandleEx(handle, _FILE_STANDARD_INFO, info, 24):
            allocation = int.from_bytes(bytes(info)[0:8], "little")
        kernel32.CloseHandle(handle)

    # Reading while refusing to share writing fails with a sharing violation
    # exactly when another handle can write it (or denies reading).
    handle = kernel32.CreateFileW(
        path,
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (None, invalid):
        open_for_writing = ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    else:
        open_for_writing = False
        kernel32.CloseHandle(handle)
    return LiveState(allocation, open_for_writing)
