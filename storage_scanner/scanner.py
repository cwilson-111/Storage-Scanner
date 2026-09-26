"""The core concurrent directory-scanning engine."""

import ctypes
import os
import queue
import stat
import sys
import threading
from typing import Optional

from storage_scanner.logging_setup import logger
from storage_scanner.models import Node
from storage_scanner.scan_progress import (
    FLUSH_EVERY_ENTRIES,
    REPORT_INTERVAL_SECONDS,
    Phase,
    WalkTracker,
)

PHASE_ADDING_UP = "Adding up folder sizes"

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
        low = ctypes.windll.kernel32.GetCompressedFileSizeW(path, None)
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
        # High 32 bits aren't retrievable without a second out-param this
        # call doesn't use; files large enough for that to matter are rare
        # enough here that the logical size fallback is an acceptable trade.

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
            low = -(-low // cluster_size) * cluster_size
        return low
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


def _worker_count():
    # Measured, not assumed: profiling scan() against both a huge system
    # directory (WinSxS, ~148k files) and a typical user directory (AppData,
    # ~534k files) on a 16-core machine showed throughput peaking around 3-5
    # worker threads and degrading steadily beyond that — 32 threads (the old
    # cpu_count*5 formula) was 30-40% *slower* than 4. Each scandir DirEntry
    # is already fully populated by Windows, so there's little real I/O wait
    # left to hide once the directory metadata is cached; extra threads past
    # a handful just add GIL/scheduling contention. Keep the pool small, with
    # a floor for low-core machines and a little headroom for slow/network
    # drives we can't profile here.
    cpu = os.cpu_count() or 4
    return min(8, max(4, cpu))


def scan(path, progress_q, cancel_event, workers=None):
    """Scan `path` concurrently, returning the root Node.

    A pool of worker threads pulls directories off a shared queue and lists
    them in parallel; each discovered sub-directory is pushed back onto the
    queue. Because workers never block waiting on each other, there is no
    risk of pool-starvation deadlock no matter how deep the tree goes.
    Sizes are rolled up afterwards in a fast in-memory pass.

    Posts progress to `progress_q` (see storage_scanner.scan_progress) and
    stops early if `cancel_event` is set: a ("walk", WalkSnapshot) every
    REPORT_INTERVAL_SECONDS from a reporter thread plus one final exact
    one once every directory has been read, then a ("phase", ...) for the
    roll-up. A directory reports its counts every FLUSH_EVERY_ENTRIES
    entries while it's still being read, so one huge folder keeps the
    totals moving instead of freezing them until it's done.

    Also posts `("root", root)` once, immediately, for a directory target
    (never for a single-file target, which returns before there's
    anything worth watching) -- a live reference to the same Node this
    function's worker threads go on to mutate in place as they walk. A
    caller (see storage_scanner.ui.main_window's live-tree preview) may
    read from it concurrently while the scan is still running: appending
    to node.children is safe to read mid-mutation under the GIL, and
    every *directory* Node's size/alloc_size/file_count stays at its
    zeroed default until _rollup() below runs once, at the very end -- a
    reader must never trust those fields as meaningful before "done" is
    posted. The walk snapshots' byte total is post-hard-link-dedup, so it
    tracks towards the same number `_rollup()` will produce.
    """
    path = os.path.abspath(path)
    name = path if path.endswith(os.sep) else os.path.basename(path) or path
    root = Node(path, name, is_dir=os.path.isdir(path))

    if not root.is_dir:
        try:
            st_info = os.stat(path)
            root.size = st_info.st_size
            root.alloc_size = _measure_alloc_size(path, st_info)
            root.mtime = st_info.st_mtime
            root.atime = st_info.st_atime
            attrs = getattr(st_info, "st_file_attributes", 0)
            root.is_cloud_placeholder = is_cloud_placeholder_attrs(attrs)
            root.file_count = 1
        except OSError:
            root.error = True
        return root

    progress_q.put(("root", root))

    n = workers or _worker_count()
    tracker = WalkTracker(n)
    work = queue.Queue()
    work.put((root, None))

    # Hard links share one (device, file-index) pair; count their bytes once
    # so a file linked into several folders doesn't inflate the total.
    seen_inodes = set()
    inode_lock = threading.Lock()

    def _publish(worker, slot, files, nbytes, entries, subdirs, finished):
        """Add counts to the tracker, then queue the subdirectories found
        since the last call -- in that order (see WalkTracker)."""
        slots = tracker.record(
            worker, slot, files, nbytes, entries, [d.name for d in subdirs], finished
        )
        for child, child_slot in zip(subdirs, slots):
            work.put((child, child_slot))

    def _scan_one(worker, node, slot):
        """List a single directory, attach children, queue sub-dirs.

        Iterates the listing as it arrives rather than list()-ing it first:
        enumerating C:\\Windows\\WinSxS\\Manifests alone takes ~8 s, and
        counting as entries arrive is what keeps a big folder's progress
        moving. An error part-way through keeps the entries read so far
        and marks the directory incomplete (node.error), like one that
        couldn't be opened at all."""
        if cancel_event.is_set():
            return
        tracker.begin(worker, slot, node.path)
        flush_every = FLUSH_EVERY_ENTRIES
        local_files = 0
        local_bytes = 0
        entries_read = 0
        subdirs = []
        try:
            with os.scandir(node.path) as listing:
                for entry in listing:
                    if cancel_event.is_set():
                        return
                    try:
                        if _IS_WINDOWS:
                            # entry.stat() on Windows never populates real
                            # st_ino/st_dev/st_nlink (always 0/0/1) -- it's
                            # built from the cheap WIN32_FIND_DATA the
                            # directory listing itself already returned,
                            # which carries no file-index or link-count info
                            # at all. A real os.stat() call is the only way
                            # to get accurate hard-link identity -- without
                            # it, hard-link dedup below silently never
                            # triggers on Windows (every ino comes back 0).
                            st_info = os.stat(entry.path, follow_symlinks=False)
                        else:
                            st_info = entry.stat(follow_symlinks=False)
                    except OSError:
                        st_info = None

                    # Reparse points (symlinks, junctions, mount points)
                    # never get traversed: their target may already be
                    # scanned elsewhere (or loop back into this tree), which
                    # would double-count size or recurse forever. They're
                    # recorded as a leaf instead.
                    attrs = getattr(st_info, "st_file_attributes", 0) if st_info is not None else 0
                    is_reparse = bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    is_placeholder = is_cloud_placeholder_attrs(attrs)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False) and not is_reparse
                    except OSError:
                        is_dir = False

                    child = Node(entry.path, entry.name, is_dir)
                    # A cloud placeholder file can also carry the
                    # reparse-point bit (OneDrive Files On-Demand uses
                    # IO_REPARSE_TAG_CLOUD) — treat it as a placeholder, not
                    # a symlink/junction, so it renders and sorts like the
                    # real file it represents rather than a link.
                    child.is_link = is_reparse and not is_placeholder
                    child.is_cloud_placeholder = is_placeholder
                    if st_info is not None:
                        child.mtime = st_info.st_mtime
                        child.atime = st_info.st_atime
                    node.children.append(child)  # only this worker touches node.children

                    if is_dir:
                        subdirs.append(child)  # queued by _publish, sized in rollup
                    else:
                        if st_info is None:
                            child.error = True
                        else:
                            size = st_info.st_size
                            alloc_size = _measure_alloc_size(entry.path, st_info)
                            ino = getattr(st_info, "st_ino", 0)
                            nlink = getattr(st_info, "st_nlink", 1)
                            if ino and nlink > 1:
                                key = (getattr(st_info, "st_dev", 0), ino)
                                with inode_lock:
                                    if key in seen_inodes:
                                        child.hardlink_dup = True
                                        size = 0
                                        alloc_size = 0
                                    else:
                                        seen_inodes.add(key)
                            child.size = size
                            child.alloc_size = alloc_size
                        child.file_count = 1
                        local_files += 1
                        local_bytes += child.size

                    entries_read += 1
                    if entries_read >= flush_every:
                        _publish(
                            worker, slot, local_files, local_bytes, entries_read, subdirs, False
                        )
                        local_files = local_bytes = entries_read = 0
                        subdirs = []
        except OSError:
            node.error = True
        _publish(worker, slot, local_files, local_bytes, entries_read, subdirs, True)

    def _worker(worker):
        while True:
            try:
                node, slot = work.get()
            except Exception:
                logger.debug("Scan worker queue.get() failed, exiting", exc_info=True)
                return
            try:
                _scan_one(worker, node, slot)
            finally:
                work.task_done()

    walk_done = threading.Event()

    def _reporter():
        while not walk_done.wait(REPORT_INTERVAL_SECONDS):
            progress_q.put(("walk", tracker.snapshot()))

    reporter = threading.Thread(target=_reporter, daemon=True)
    reporter.start()
    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    try:
        work.join()  # block until every queued directory has been processed
    finally:
        walk_done.set()
        reporter.join()
    progress_q.put(("walk", tracker.snapshot()))

    # Roll sizes/counts up the tree (iterative post-order; deep trees safe).
    progress_q.put(("phase", Phase(PHASE_ADDING_UP)))
    _rollup(root)
    return root


def find_inaccessible_paths(root):
    """Every node in `root`'s tree with node.error=True -- a directory
    that couldn't be listed (permission denied, e.g. C:\\System Volume
    Information, or a vendor backup tool's own locked-down snapshot
    folder -- being an Administrator doesn't automatically grant access to
    a folder whose ACL excludes the Administrators group entirely) or a
    file whose metadata couldn't be read.

    This is the only way to discover those paths after a scan: a directory
    node with error=True has no children at all when it couldn't be opened
    (or only the entries read before a mid-listing failure, see _scan_one
    above), so the rest of its subtree is silently absent from the tree --
    not sized as 0 by mistake, genuinely never counted. Surfacing the
    *paths* lets a user recognize a familiar
    culprit (System Volume Information, a backup tool's own storage) and
    decide what to do about it themselves; there's no reliable way to
    estimate how large an unreadable directory actually is without being
    able to read it.
    """
    errors = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.error:
            errors.append(node)
        if node.is_dir:
            stack.extend(node.children)
    return errors


def _rollup(root):
    """Sum child sizes/file counts into each directory, bottom-up.

    A directory also inherits `error=True` from any child that couldn't be
    fully read (an unreadable subdirectory, or a file whose stat() failed)
    -- without this, a permission-denied folder deep in the tree left every
    ancestor's total silently understated by that whole subtree's size,
    with the ⚠ warning icon (see main_window._insert_node) shown only on
    the one row that actually failed, invisible unless a user happened to
    have that exact row expanded. Post-order traversal means a grandchild's
    error is already folded into its parent by the time the parent's own
    children are summed into the grandparent, so this propagates all the
    way to the root in one pass.
    """
    stack = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if not node.is_dir:
            continue
        if processed:
            for child in node.children:
                node.size += child.size
                node.alloc_size += child.alloc_size
                node.file_count += child.file_count
                if child.error:
                    node.error = True
        else:
            stack.append((node, True))
            for child in node.children:
                if child.is_dir:
                    stack.append((child, False))
