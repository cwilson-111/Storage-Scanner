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
    """Whether `attrs` mark an online-only placeholder (OneDrive Files
    On-Demand and other cloud-sync or HSM tools).

    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS alone is enough: it means the
    file's data isn't on this disk. A real OneDrive file not downloaded
    here lists as 0x400020 to os.stat and os.scandir with no reparse bit,
    since the cloud filter hides it from ordinary callers (Turbo Scan,
    reading the MFT, sees it; checklist run 4, 2026-10-06).

    RECALL_ON_OPEN and OFFLINE count only on a reparse point. CompactOS/
    WIMBoot-compressed system files also set RECALL_ON_OPEN on-disk ("decompress
    from the WIM on open"), unrelated to cloud sync, and are never reparse
    points -- Turbo Scan's raw $STANDARD_INFORMATION read showed thousands
    of ordinary C:\\Windows\\Boot files as RECALL_ON_OPEN with no
    REPARSE_POINT bit at all, none of them cloud placeholders.
    """
    if attrs & _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
        return True
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


_FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
_FILE_READ_ATTRIBUTES = 0x80
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_ERROR_MORE_DATA = 234
_EXTENT_BUFFER_BYTES = 64 * 1024


def _sparse_alloc_size(path):
    """Bytes NTFS has allocated to a sparse file, from its cluster map
    (FSCTL_GET_RETRIEVAL_POINTERS: every extent that isn't a hole), or None
    when that can't be read -- including a file with no clusters at all,
    whose data lives in its MFT record.

    GetCompressedFileSizeW is no good here: it never answers more than the
    logical size, while NTFS gives a sparse file whole 64 KiB compression
    units. Measured on a real C: (2026-10-06): a 1,615-byte sparse file
    (an Ollama blob) has 65,536 bytes allocated and GetCompressedFileSizeW
    says 1,615; a 4,661,211,424-byte one has 4,661,248,000. Turbo Scan,
    which reads the MFT, already counted these right. A 200 MB file with
    10 bytes written and every other byte punched out as a hole
    (FSCTL_SET_ZERO_DATA) reads 65,536."""
    from ctypes import wintypes

    cluster_size = _get_cluster_size(path)
    if not cluster_size:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]  # fmt: skip
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(
        path,
        _FILE_READ_ATTRIBUTES,
        _SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        clusters, next_vcn = 0, 0
        out = ctypes.create_string_buffer(_EXTENT_BUFFER_BYTES)
        while True:
            start = ctypes.c_longlong(next_vcn)
            returned = wintypes.DWORD()
            ok = kernel32.DeviceIoControl(
                handle,
                _FSCTL_GET_RETRIEVAL_POINTERS,
                ctypes.byref(start),
                8,
                out,
                _EXTENT_BUFFER_BYTES,
                ctypes.byref(returned),
                None,
            )
            if not ok and ctypes.get_last_error() != _ERROR_MORE_DATA:
                return None  # no clusters (resident), or unreadable
            # RETRIEVAL_POINTERS_BUFFER: ExtentCount, padding, StartingVcn,
            # then (NextVcn, Lcn) pairs; Lcn -1 is a hole.
            count = int.from_bytes(out.raw[0:4], "little")
            if not count:
                break
            vcn = int.from_bytes(out.raw[8:16], "little", signed=True)
            for i in range(count):
                at = 16 + i * 16
                extent_end = int.from_bytes(out.raw[at : at + 8], "little", signed=True)
                lcn = int.from_bytes(out.raw[at + 8 : at + 16], "little", signed=True)
                if lcn != -1:
                    clusters += extent_end - vcn
                vcn = extent_end
            next_vcn = vcn
            if ok:
                break
        return clusters * cluster_size
    finally:
        kernel32.CloseHandle(handle)


def _measure_alloc_size(path, st_info):
    """Actual on-disk bytes for a file, cross-platform.

    POSIX systems already report this directly via `st_blocks` (512-byte
    units) — that alone correctly reflects sparse files. Windows has no
    such field, so it needs its own API call: a sparse file's cluster map
    (_sparse_alloc_size), else GetCompressedFileSizeW. A cloud placeholder
    is never opened, so nothing can make it download.
    """
    if _IS_WINDOWS:
        attrs = getattr(st_info, "st_file_attributes", 0)
        if (
            attrs & stat.FILE_ATTRIBUTE_SPARSE_FILE
            and not attrs & stat.FILE_ATTRIBUTE_COMPRESSED
            and not is_cloud_placeholder_attrs(attrs)
        ):
            try:
                allocated = _sparse_alloc_size(path)
            except OSError:
                allocated = None
            if allocated is not None:
                return allocated
        return _windows_alloc_size(path, st_info.st_size)
    st_blocks = getattr(st_info, "st_blocks", None)
    return st_blocks * 512 if st_blocks is not None else st_info.st_size


def _link_alloc_size(path, st_info):
    """On-disk bytes of a link itself (symbolic link, junction, mount point),
    from its own lstat `st_info`, never its target's.

    GetCompressedFileSizeW and the cluster-map query both open by path and
    so follow a symbolic link: a glog `<program>.INFO` link in
    Windows\\Temp was billed its target log's 4,096 bytes while Turbo Scan,
    reading the link's own empty $DATA, said 0 (checklist runs 4 and 5,
    2026-10-06). A link's $DATA is normally empty, so this is 0; one that
    holds data is rounded up to whole clusters like any ordinary file."""
    if not _IS_WINDOWS:
        return _measure_alloc_size(path, st_info)
    size = st_info.st_size
    cluster_size = _get_cluster_size(path) if size else None
    return -(-size // cluster_size) * cluster_size if cluster_size else size
