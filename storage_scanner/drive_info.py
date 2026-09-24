"""Windows drive-type and filesystem detection for Turbo Scan eligibility.

Pure stdlib ctypes, matching the rest of the codebase's zero-third-party-
runtime-dependency policy (see scanner.py/file_ops.py). Every public
function here follows their convention of never raising on a Win32 call
failure -- a capability probe failing just means "not eligible", not a
reason to crash the whole scan.
"""

import ctypes
import os

from storage_scanner.platform_support import IS_WINDOWS

# GetDriveTypeW return values (winbase.h).
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT_DIR = 1
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_DRIVE_RAMDISK = 6

# Comfortably longer than any real Windows filesystem name ("NTFS", "FAT32",
# "exFAT", "ReFS", ...).
_MAX_FILESYSTEM_NAME_LENGTH = 32


def get_volume_root(path):
    """The root path of the volume containing `path` (e.g. "C:\\\\" for a
    drive, or "\\\\server\\share\\" for a UNC path -- both are valid inputs
    to GetDriveTypeW/GetVolumeInformationW)."""
    drive, _tail = os.path.splitdrive(os.path.abspath(path))
    return drive + "\\"


def _get_drive_type(root_path):
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(root_path)
    except OSError:
        return _DRIVE_UNKNOWN


def _get_filesystem_name(root_path):
    try:
        fs_name_buffer = ctypes.create_unicode_buffer(_MAX_FILESYSTEM_NAME_LENGTH)
        succeeded = ctypes.windll.kernel32.GetVolumeInformationW(
            root_path,
            None,
            0,
            None,
            None,
            None,
            fs_name_buffer,
            _MAX_FILESYSTEM_NAME_LENGTH,
        )
        if not succeeded:
            return None
        return fs_name_buffer.value
    except OSError:
        return None


def is_ntfs_fixed_drive(path):
    """True if `path` lives on a local, fixed NTFS volume -- the only kind
    Turbo Scan supports (per the roadmap: NTFS only, with the existing
    directory-walking engine handling network shares, removable media, and
    other filesystems). Never raises: any failure to query the drive is
    treated as "not eligible", sending the caller to the Compatible engine
    rather than crashing over what's just a capability probe.
    """
    if not IS_WINDOWS:
        return False
    try:
        root_path = get_volume_root(path)
    except (OSError, ValueError):
        return False
    if _get_drive_type(root_path) != _DRIVE_FIXED:
        return False
    # GetVolumeInformationW always reports the name in upper case per MSDN.
    return _get_filesystem_name(root_path) == "NTFS"
