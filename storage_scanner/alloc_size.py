"""What a file really occupies on disk, and whether it's an online-only
cloud placeholder that occupies almost nothing there.

The Compatible engine (scanner.scan) measures every file it lists with
_measure_alloc_size. The MFT engine reads sizes from the records instead,
but asks is_cloud_placeholder_attrs the same question (see mft_parser), so
both engines agree on what a placeholder is.
"""

import ctypes
import os
import stat
import sys
import threading
from typing import Optional

_IS_WINDOWS = sys.platform == "win32"

# OneDrive Files On-Demand (and similar cloud-sync clients) mark an
# online-only placeholder with these attribute bits; the file's normal name
# and full logical size are still visible, but almost nothing is allocated
# on local disk until it's opened. FILE_ATTRIBUTE_OFFLINE covers older
# HSM/cloud-sync tools that predate Files On-Demand.
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_FILE_ATTRIBUTE_OFFLINE = 0x00001000
_CLOUD_PLACEHOLDER_ATTRS = (
    _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | _FILE_ATTRIBUTE_OFFLINE
)


def is_cloud_placeholder_attrs(attrs):
    """True only when `attrs` both carries a placeholder-recall bit AND is a
    reparse point -- every real Cloud Files API placeholder (OneDrive Files
    On-Demand included) is implemented as an IO_REPARSE_TAG_CLOUD reparse
    point, no exceptions. Requiring it here rules out a real false positive
    found on a real machine: CompactOS/WIMBoot-compressed system files also
    set FILE_ATTRIBUTE_RECALL_ON_OPEN on-disk (to mark "decompress from the
    WIM on open"), unrelated to cloud sync, but are never reparse points --
    confirmed via Turbo Scan's raw $STANDARD_INFORMATION read showing
    thousands of ordinary C:\\Windows\\Boot files as RECALL_ON_OPEN with no
    REPARSE_POINT bit at all, none of which are actually cloud placeholders.
    """
    return bool(attrs & _CLOUD_PLACEHOLDER_ATTRS) and bool(
        attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


_INVALID_FILE_SIZE = 0xFFFFFFFF

# GetCompressedFileSizeW-per-volume cluster size, cached so an entire scan
# costs one extra GetDiskFreeSpaceW call per drive, not one per file.
# A None value is cached too: it means "asked the OS, it wouldn't say", so a
# repeat lookup for that volume doesn't re-ask on every single file.
_cluster_size_cache: "dict[str, Optional[int]]" = {}
_cluster_size_cache_lock = threading.Lock()


def _volume_root(path):
    drive, _tail = os.path.splitdrive(os.path.abspath(path))
    return drive + "\\" if drive else None


def _get_cluster_size(path):
    """Bytes per allocation unit (cluster) for the volume containing
    `path`, or None if it can't be determined (in which case the caller
    should skip rounding rather than guess)."""
    volume_root = _volume_root(path)
    if not volume_root:
        return None
    with _cluster_size_cache_lock:
        if volume_root in _cluster_size_cache:
            return _cluster_size_cache[volume_root]
    size = None
    try:
        sectors_per_cluster = ctypes.c_ulong(0)
        bytes_per_sector = ctypes.c_ulong(0)
        free_clusters = ctypes.c_ulong(0)
        total_clusters = ctypes.c_ulong(0)
        succeeded = ctypes.windll.kernel32.GetDiskFreeSpaceW(
            volume_root,
            ctypes.byref(sectors_per_cluster),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free_clusters),
            ctypes.byref(total_clusters),
        )
        if succeeded:
            size = sectors_per_cluster.value * bytes_per_sector.value
    except OSError:
        size = None
    with _cluster_size_cache_lock:
        _cluster_size_cache[volume_root] = size
    return size


def _windows_alloc_size(path, fallback):
    """Actual on-disk bytes for `path`, accounting for NTFS compression and
    sparse files — logical `st_size` alone overstates disk usage for both.

    Uses GetCompressedFileSizeW (stdlib ctypes, no extra dependency). Falls
    back to `fallback` (the logical size) if the call fails for any reason
    (permissions, exotic filesystem, etc.) — better an approximate number
    than a crashed scan.
    """
    try:
        # The on-disk size only needs its upper 32 bits for a file of 4 GiB
        # or more: it's the logical size for an ordinary file and less for
        # a compressed or sparse one. Asking for them costs an out-parameter
        # per call, so smaller files skip it.
        high = ctypes.c_ulong(0) if fallback >> 32 else None
        low = ctypes.windll.kernel32.GetCompressedFileSizeW(
            path, None if high is None else ctypes.byref(high)
        )
        # ctypes defaults a windll call's return type to signed c_int;
        # GetCompressedFileSizeW's real return is an unsigned DWORD, so a
        # low-DWORD value >= 0x80000000 (a compressed size with that bit
        # set, or the INVALID_FILE_SIZE sentinel 0xFFFFFFFF itself) comes
        # back here as a negative Python int -- fold it back into the
        # correct unsigned 32-bit value before using it for anything.
        # Correcting `low` itself (not just the comparison below) matters:
        # it's also used for the cluster-rounding math and final return a
        # few lines down, so a masked-only comparison would still hand
        # back a corrupted negative alloc_size for a large compressed file
        # that isn't actually the INVALID_FILE_SIZE failure case.
        if low < 0:
            low &= 0xFFFFFFFF
        if low == _INVALID_FILE_SIZE and ctypes.GetLastError() != 0:
            return fallback
        # Without the upper 32 bits every file over 4 GiB was billed its
        # size modulo 4 GiB: a 9.0 GB game archive read as 0.4 GB on disk.
        size = low if high is None else (high.value << 32) | low

        # GetCompressedFileSizeW only returns something smaller than the
        # logical size for a genuinely compressed or sparse file -- for an
        # ordinary file it just echoes the logical size back, uncorrected
        # for the fact that NTFS can only ever allocate whole clusters.
        # Round up to the volume's real cluster size to match true
        # physical disk usage (and what Explorer's own "Size on disk"
        # property shows) -- confirmed against Turbo Scan's own
        # allocation accounting, which reads it directly from the MFT.
        # (A handful of very small files stored resident, inline in their
        # MFT record with no separate cluster allocation at all, will
        # still get rounded up here since there's no cheap way for this
        # API-based path to know a file is resident -- a known, minor,
        # accepted imprecision, not worth a costlier check to eliminate.)
        cluster_size = _get_cluster_size(path)
        if cluster_size:
            size = -(-size // cluster_size) * cluster_size
        return size
    except OSError:
        return fallback


def _measure_alloc_size(path, st_info):
    """Actual on-disk bytes for a file, cross-platform.

    POSIX systems already report this directly via `st_blocks` (512-byte
    units) — that alone correctly reflects sparse files. Windows has no
    such field, so it needs its own API call.
    """
    if _IS_WINDOWS:
        return _windows_alloc_size(path, st_info.st_size)
    st_blocks = getattr(st_info, "st_blocks", None)
    return st_blocks * 512 if st_blocks is not None else st_info.st_size
