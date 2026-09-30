"""The Windows Recycle Bin: what it can't hold, sending things to it, and
checking they arrived.

SHFileOperationW with FOF_ALLOWUNDO sends an item to the Recycle Bin when
it can, and when it can't it deletes the item permanently instead, and
still returns success (Microsoft's SHFILEOPSTRUCTW docs). Measured on this
project's Windows 11 machine, it can't for:

- anything on a subst drive (GetDriveTypeW calls those fixed drives; only
  QueryDosDeviceW or SHQueryRecycleBinW on the root gives them away), a
  network drive or share, or a removable drive;
- a volume whose Recycle Bin is turned off (NukeOnDelete);
- a file or folder whose own path is 260 characters or more, or a folder
  holding anything whose path is 259 or more (258 inside a folder went to
  the bin, 259 didn't; 259 for the item itself did, 260 didn't) -- the
  length where the item ends up in the bin doesn't matter;
- [documented, not measured] anything bigger than the bin's maximum size.

So every delete is checked here first (bin_blockers), and the caller asks
before deleting permanently. As a backstop for a case this misses, recycle()
passes FOF_WANTNUKEWARNING, so Windows itself asks "permanently delete?";
that flag has no effect alongside FOF_NOCONFIRMATION (measured: silently
deleted), so FOF_NOCONFIRMATION isn't passed -- a user who turned on the
Recycle Bin's own "Display delete confirmation dialog" sees that dialog as
well. Answering No aborts (fAnyOperationsAborted). And an item that's gone
afterwards is looked up in the bin by its original path ($I files), so a
Yes there is recorded as a permanent delete, not a recycle.

Every Win32 call is made inside a function, so this imports anywhere.
"""

import contextlib
import ctypes
import os
import stat
import struct
import time
from ctypes import wintypes

from storage_scanner.delete_outcome import (
    DELETED_PERMANENTLY,
    FAILED,
    RECYCLED,
    REFUSED,
    UNVERIFIED,
)
from storage_scanner.formatting import human_size

# Measured limits (see the module docstring).
LONGEST_RECYCLABLE_PATH = 259  # the item itself
LONGEST_RECYCLABLE_INSIDE = 258  # anything inside a folder being recycled

_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040  # the bit that routes deletes to the Recycle Bin
_FOF_NOERRORUI = 0x0400
_FOF_WANTNUKEWARNING = 0x4000
_FOF_NORECURSEREPARSE = 0x8000  # don't follow into a junction/symlink's target
_RECYCLE_FLAGS = (
    _FOF_ALLOWUNDO | _FOF_WANTNUKEWARNING | _FOF_SILENT | _FOF_NOERRORUI | _FOF_NORECURSEREPARSE
)
_PERMANENT_FLAGS = _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI | _FOF_NORECURSEREPARSE

_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_REMOVABLE = 2
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_BIN_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
# How far before the delete call a $I file's timestamp may be and still
# count as this delete's (clock granularity, not a race allowance).
_BIN_CLOCK_SLACK_SECONDS = 5


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),  # FILEOP_FLAGS is a WORD
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def extended_path(path):
    """`path` as a \\\\?\\ path, which Win32 takes past MAX_PATH as-is."""
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def exists(path):
    """os.path.lexists, but right for paths of any length."""
    return os.path.lexists(extended_path(path))


def volume_root(path):
    """The root of the volume holding `path` ("C:\\", a mounted folder's
    path, or a share root), where that volume's Recycle Bin lives."""
    buffer = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetVolumePathNameW(os.path.abspath(path), buffer, 1024):
        return buffer.value
    drive, _tail = os.path.splitdrive(os.path.abspath(path))
    return drive + "\\"


def _subst_target(drive):
    """The folder a subst drive letter ("Q:") stands for, else None."""
    if len(drive) != 2 or drive[1] != ":":
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.QueryDosDeviceW(drive, buffer, 1024):
        return None
    return buffer.value[4:] if buffer.value.startswith("\\??\\") else None


def _bin_settings(root):
    """(NukeOnDelete, MaxCapacity in bytes) from this user's Recycle Bin
    settings for the volume at `root`; either is None when not recorded."""
    import winreg  # Windows-only module

    buffer = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, 64):
        return None, None
    name = buffer.value
    guid = name[name.find("{") : name.find("}") + 1]
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{_BIN_SETTINGS_KEY}\{guid}") as key:
            for value_name in ("NukeOnDelete", "MaxCapacity"):
                with contextlib.suppress(OSError):
                    values[value_name] = winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return None, None
    capacity_mb = values.get("MaxCapacity")
    capacity = capacity_mb * 1024 * 1024 if capacity_mb is not None else None
    nuke = values.get("NukeOnDelete")
    return (bool(nuke) if nuke is not None else None), capacity


def _has_recycle_bin(root):
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    return ctypes.windll.shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) == 0


def _drive_type(root):
    return ctypes.windll.kernel32.GetDriveTypeW(root)


def _volume_blockers(drive, root, nuke_on_delete):
    drive_type = _drive_type(root)
    if drive_type == _DRIVE_REMOTE:
        return [f"it's on a network drive ({drive}), and network drives have no Recycle Bin"]
    if drive_type == _DRIVE_REMOVABLE:
        return [f"it's on a removable drive ({drive}), and removable drives have no Recycle Bin"]
    if drive_type != _DRIVE_FIXED or not _has_recycle_bin(root):
        return [f"Windows has no Recycle Bin for {root}"]
    if nuke_on_delete:
        return [
            f"the Recycle Bin is turned off for {root} (its properties say \"Don't move "
            'files to the Recycle Bin")'
        ]
    return []


def _walk(path):
    """(longest path length, total bytes) of everything inside folder
    `path`, not following junctions or links -- the shell removes a link
    itself, never what it points to."""
    ext_root = extended_path(path)
    base_length = len(os.path.abspath(path))
    longest = total = 0
    stack = [ext_root]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                longest = max(longest, base_length + len(entry.path) - len(ext_root))
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                else:
                    total += info.st_size
    return longest, total


def bin_blockers(path, is_dir):
    """Why the Recycle Bin can't take `path`, as short clauses ("it's on a
    network drive..."); empty if it can. A folder is walked on disk, since
    anything added inside it since the scan counts too."""
    full = os.path.abspath(path)
    drive, _tail = os.path.splitdrive(full)
    # Both answered from the path alone, before anything touches a share.
    if drive.startswith("\\\\"):
        return [f"it's on a network share ({drive}), and network shares have no Recycle Bin"]
    target = _subst_target(drive)
    if target:
        return [
            f"{drive} is a subst drive (a drive letter standing in for {target}), and "
            "subst drives have no Recycle Bin"
        ]
    root = volume_root(full)
    nuke_on_delete, capacity = _bin_settings(root)
    blockers = _volume_blockers(drive, root, nuke_on_delete)
    if blockers:
        return blockers
    if len(full) > LONGEST_RECYCLABLE_PATH:
        blockers.append(
            f"its path is {len(full)} characters long, and the Recycle Bin only takes "
            f"paths up to {LONGEST_RECYCLABLE_PATH}"
        )
    if is_dir:
        longest, size = _walk(path)
        if longest > LONGEST_RECYCLABLE_INSIDE:
            blockers.append(
                f"it holds a path {longest} characters long, and the Recycle Bin only "
                f"takes paths up to {LONGEST_RECYCLABLE_INSIDE} from inside a folder"
            )
    else:
        try:
            size = os.stat(extended_path(path)).st_size
        except OSError:
            size = 0
    if capacity is not None and size > capacity:
        blockers.append(
            f"it's {human_size(size)}, more than the {human_size(capacity)} the Recycle "
            f"Bin on {root} can hold"
        )
    return blockers


def _shell_delete(path, flags):
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = path + "\x00\x00"  # pFrom must be double-NUL terminated
    op.pTo = None
    op.fFlags = flags
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result, bool(op.fAnyOperationsAborted)


def _read_info_file(path):
    """The original path a $I file records, or None if it can't be read."""
    try:
        with open(path, "rb") as f:
            data = f.read(4096)
        (version,) = struct.unpack_from("<q", data, 0)
        if version == 2:  # Windows 10 and later
            (length,) = struct.unpack_from("<i", data, 24)
            raw = data[28 : 28 + 2 * length]
        elif version == 1:  # Vista to 8.1: a fixed MAX_PATH field
            raw = data[24 : 24 + 520]
        else:
            return None
    except (OSError, struct.error):
        return None
    return raw.decode("utf-16-le", errors="replace").split("\x00")[0]


def recycled_entries(root, since=None):
    """[(original path, $I file, $R item), ...] for this user's items in the
    Recycle Bin of the volume at `root` -- only those put there at or after
    time.time() value `since`, if given. None if the bin can't be read."""
    readable = False
    found = []
    try:
        sid_folders = [entry.path for entry in os.scandir(os.path.join(root, "$Recycle.Bin"))]
    except OSError:
        return None
    for folder in sid_folders:
        try:
            items = list(os.scandir(folder))
        except OSError:
            continue  # another user's
        readable = True
        for item in items:
            if not item.name.startswith("$I"):
                continue
            if since is not None:
                try:
                    if item.stat().st_mtime < since - _BIN_CLOCK_SLACK_SECONDS:
                        continue
                except OSError:
                    continue
            original = _read_info_file(item.path)
            if original:
                found.append((original, item.path, os.path.join(folder, "$R" + item.name[2:])))
    return found if readable else None


def recycle(path):
    """Send `path` to the Recycle Bin. Returns delete_outcome RECYCLED,
    DELETED_PERMANENTLY (gone, but not in the bin: someone answered Yes to
    Windows' own "permanently delete?"), UNVERIFIED (gone, bin unreadable),
    REFUSED (that question answered No) or FAILED."""
    full = os.path.abspath(path)
    started = time.time()
    _result, aborted = _shell_delete(full, _RECYCLE_FLAGS)
    if exists(full):
        return REFUSED if aborted else FAILED
    entries = recycled_entries(volume_root(full), since=started)
    if entries is None:
        return UNVERIFIED
    key = os.path.normcase(full)
    if any(os.path.normcase(original) == key for original, _info, _item in entries):
        return RECYCLED
    return DELETED_PERMANENTLY


def delete_permanently(path):
    """Delete `path` outright -- only ever after the user confirmed it."""
    full = os.path.abspath(path)
    _shell_delete(full, _PERMANENT_FLAGS)
    return FAILED if exists(full) else DELETED_PERMANENTLY
