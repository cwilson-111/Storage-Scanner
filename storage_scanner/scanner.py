"""The core concurrent directory-scanning engine."""

import os
import queue
import stat
import sys
import threading

from storage_scanner.alloc_size import _measure_alloc_size, is_cloud_placeholder_attrs
from storage_scanner.logging_setup import logger
from storage_scanner.models import (
    FLAG_CLOUD_PLACEHOLDER,
    FLAG_ERROR,
    FLAG_HARDLINK_DUP,
    FLAG_LINK,
    FileNode,
    Node,
    detached_file,
)
from storage_scanner.scan_progress import (
    FLUSH_EVERY_ENTRIES,
    REPORT_INTERVAL_SECONDS,
    Phase,
    WalkTracker,
)

PHASE_ADDING_UP = "Adding up folder sizes"

_IS_WINDOWS = sys.platform == "win32"


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


def _needs_literal_path(name):
    """Win32 strips a trailing dot or space from every path it's handed, so
    `foo.` reads as `foo` -- another folder, if both exist."""
    return name[-1:] in (".", " ")


def _literal_path(path):
    r"""`path` in the \\?\ form Win32 passes through untouched."""
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _scan_file(path):
    """The FileNode for a scan target that is itself a file."""
    try:
        st_info = os.stat(path)
    except OSError:
        return detached_file(path, flags=FLAG_ERROR)
    attrs = getattr(st_info, "st_file_attributes", 0)
    return detached_file(
        path,
        st_info.st_size,
        _measure_alloc_size(path, st_info),
        st_info.st_mtime,
        st_info.st_atime,
        FLAG_CLOUD_PLACEHOLDER if is_cloud_placeholder_attrs(attrs) else 0,
    )


def scan(path, progress_q, cancel_event, workers=None):
    """Scan `path` concurrently, returning the root Node (a FileNode when
    `path` is a file).

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

    Also posts `("live_tree", tracker)` once, before reading anything,
    for a directory target (never for a single-file target, which returns
    before there's anything worth watching): the WalkTracker of the same
    Node tree this function's worker threads go on to build in place. A
    caller (see storage_scanner.ui.live_tree) may read the tree while the
    scan is still running: appending to node.dirs and node's file rows is
    safe to read mid-mutation under the GIL (see models.Node.add_file for
    the row order that makes it so), and a *directory* Node's sizes and
    file and folder counts are its running totals so far -- read them
    through tracker.folders(), which also says whether it's done. _rollup()
    recomputes them from the file rows once the walk ends, so the returned
    tree's numbers are exactly what they'd be without the live totals.
    """
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return _scan_file(path)
    name = path if path.endswith(os.sep) else os.path.basename(path) or path
    root = Node(path, name)

    n = workers or _worker_count()
    tracker = WalkTracker(root, n)
    progress_q.put(("live_tree", tracker))
    work = queue.Queue()
    work.put((root, ()))

    # Hard links share one (device, file-index) pair; count their bytes once
    # so a file linked into several folders doesn't inflate the total.
    seen_inodes = set()
    inode_lock = threading.Lock()

    def _publish(worker, node, chain, files, nbytes, nalloc, entries, subdirs, finished):
        """Add counts to the tracker, then queue the subdirectories found
        since the last call -- in that order (see WalkTracker)."""
        tracker.record(worker, node, chain, files, nbytes, nalloc, entries, subdirs, finished)
        if subdirs:
            child_chain = (*chain, node)
            for child in subdirs:
                work.put((child, child_chain))

    def _scan_one(worker, node, chain):
        """List a single directory, attach its subfolders and file rows,
        queue sub-dirs. `chain` is every folder above `node`, root first.

        Iterates the listing as it arrives rather than list()-ing it first:
        enumerating C:\\Windows\\WinSxS\\Manifests alone takes ~8 s, and
        counting as entries arrive is what keeps a big folder's progress
        moving. An error part-way through keeps the entries read so far
        and marks the directory incomplete (node.error), like one that
        couldn't be opened at all."""
        if cancel_event.is_set():
            return
        tracker.begin(worker, node)
        flush_every = FLUSH_EVERY_ENTRIES
        local_files = 0
        local_bytes = 0
        local_alloc = 0
        entries_read = 0
        subdirs = []
        # A folder with a part ending in a dot or space (anywhere above it)
        # is listed and read through its literal path; the tree keeps the
        # ordinary one.
        literal = _IS_WINDOWS and any(_needs_literal_path(part) for part in node.path.split(os.sep))
        try:
            with os.scandir(_literal_path(node.path) if literal else node.path) as listing:
                for entry in listing:
                    if cancel_event.is_set():
                        return
                    if literal:
                        entry_path = os.path.join(node.path, entry.name)
                        fs_path = entry.path
                    else:
                        entry_path = fs_path = entry.path
                        if _IS_WINDOWS and _needs_literal_path(entry.name):
                            fs_path = _literal_path(entry_path)
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
                            st_info = os.stat(fs_path, follow_symlinks=False)
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

                    if is_dir:
                        child = Node(entry_path, entry.name)
                        child.is_cloud_placeholder = is_placeholder
                        if st_info is not None:
                            child.mtime = st_info.st_mtime
                            child.atime = st_info.st_atime
                        node.dirs.append(child)  # only this worker touches node's children
                        subdirs.append(child)  # queued by _publish, sized in rollup
                    else:
                        # A cloud placeholder file can also carry the
                        # reparse-point bit (OneDrive Files On-Demand uses
                        # IO_REPARSE_TAG_CLOUD) — treat it as a placeholder,
                        # not a symlink/junction, so it renders and sorts
                        # like the real file it represents rather than a link.
                        if is_placeholder:
                            flags = FLAG_CLOUD_PLACEHOLDER
                        else:
                            flags = FLAG_LINK if is_reparse else 0
                        if st_info is None:
                            node.add_file(entry.name, flags=flags | FLAG_ERROR)
                        else:
                            size = st_info.st_size
                            alloc_size = _measure_alloc_size(fs_path, st_info)
                            ino = getattr(st_info, "st_ino", 0)
                            nlink = getattr(st_info, "st_nlink", 1)
                            if ino and nlink > 1:
                                key = (getattr(st_info, "st_dev", 0), ino)
                                with inode_lock:
                                    if key in seen_inodes:
                                        flags |= FLAG_HARDLINK_DUP
                                        size = 0
                                        alloc_size = 0
                                    else:
                                        seen_inodes.add(key)
                            node.add_file(
                                entry.name,
                                size,
                                alloc_size,
                                st_info.st_mtime,
                                st_info.st_atime,
                                flags,
                            )
                            local_bytes += size
                            local_alloc += alloc_size
                        local_files += 1

                    entries_read += 1
                    if entries_read >= flush_every:
                        _publish(
                            worker,
                            node,
                            chain,
                            local_files,
                            local_bytes,
                            local_alloc,
                            entries_read,
                            subdirs,
                            False,
                        )
                        local_files = local_bytes = local_alloc = entries_read = 0
                        subdirs = []
        except OSError:
            node.error = True
        _publish(
            worker, node, chain, local_files, local_bytes, local_alloc, entries_read, subdirs, True
        )

    def _worker(worker):
        while True:
            try:
                item = work.get()
            except Exception:
                logger.debug("Scan worker queue.get() failed, exiting", exc_info=True)
                return
            if item is None:  # the walk is over
                return
            try:
                _scan_one(worker, *item)
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
        # Let the workers go: a worker left waiting on the queue would keep
        # its closures -- the tracker, and through it the whole tree -- alive
        # for as long as the app runs, one more tree for every scan.
        for _ in threads:
            work.put(None)
        for t in threads:
            t.join()
    progress_q.put(("walk", tracker.snapshot()))

    # Roll sizes/counts up the tree (iterative post-order; deep trees safe),
    # replacing the walk's running totals with sums of the file rows.
    progress_q.put(("phase", Phase(PHASE_ADDING_UP)))
    _rollup(root, own_sizes=False)
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
    if not root.is_dir:
        return [root] if root.error else []
    errors = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.error:
            errors.append(node)
        flags = node.file_flags
        errors.extend(FileNode(node, i) for i in node.file_rows() if flags[i] & FLAG_ERROR)
        stack.extend(node.dirs)
    return errors


def _rollup(root, own_sizes=True):
    """Sum file rows and subfolder totals into each directory, bottom-up,
    on a freshly built tree (no removed rows yet). With `own_sizes` a
    folder's own size fields are added to (the MFT engine sets them to its
    directory record's size); without, they're replaced (scan() leaves the
    walk's running totals there).

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
    if not root.is_dir:
        return
    stack = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if processed:
            size = sum(node.file_sizes)
            alloc_size = sum(node.file_allocs)
            file_count = len(node.file_names)
            folder_count = len(node.dirs)
            error = any(flags & FLAG_ERROR for flags in node.file_flags)
            for child in node.dirs:
                size += child.size
                alloc_size += child.alloc_size
                file_count += child.file_count
                folder_count += child.folder_count
                error = error or child.error
            if own_sizes:
                size += node.size
                alloc_size += node.alloc_size
                file_count += node.file_count
            node.size = size
            node.alloc_size = alloc_size
            node.file_count = file_count
            node.folder_count = folder_count  # no engine sets a folder's own
            if error:
                node.error = True
        else:
            stack.append((node, True))
            for child in node.dirs:
                stack.append((child, False))
