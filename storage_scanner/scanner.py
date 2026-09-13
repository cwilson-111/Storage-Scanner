"""The core concurrent directory-scanning engine."""

import os
import queue
import stat
import threading

from storage_scanner.logging_setup import logger
from storage_scanner.models import Node


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

    Posts the running file count to `progress_q` and stops early if
    `cancel_event` is set.
    """
    path = os.path.abspath(path)
    name = path if path.endswith(os.sep) else os.path.basename(path) or path
    root = Node(path, name, is_dir=os.path.isdir(path))

    if not root.is_dir:
        try:
            root.size = os.path.getsize(path)
            root.file_count = 1
        except OSError:
            root.error = True
        progress_q.put(("progress", root.file_count))
        return root

    work = queue.Queue()
    work.put(root)

    scanned = [0]
    counter_lock = threading.Lock()

    # Hard links share one (device, file-index) pair; count their bytes once
    # so a file linked into several folders doesn't inflate the total.
    seen_inodes = set()
    inode_lock = threading.Lock()

    def _scan_one(node):
        """List a single directory, attach children, queue sub-dirs."""
        if cancel_event.is_set():
            return
        try:
            entries = list(os.scandir(node.path))
        except OSError:
            node.error = True
            return

        local_files = 0
        for entry in entries:
            if cancel_event.is_set():
                return
            try:
                st_info = entry.stat(follow_symlinks=False)
            except OSError:
                st_info = None

            # Reparse points (symlinks, junctions, mount points) never get
            # traversed: their target may already be scanned elsewhere (or
            # loop back into this tree), which would double-count size or
            # recurse forever. They're recorded as a leaf instead.
            is_reparse = bool(
                st_info is not None
                and getattr(st_info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            try:
                is_dir = entry.is_dir(follow_symlinks=False) and not is_reparse
            except OSError:
                is_dir = False

            child = Node(entry.path, entry.name, is_dir)
            child.is_link = is_reparse
            node.children.append(child)  # only this worker touches node.children

            if is_dir:
                work.put(child)          # discovered later, sized in rollup
            else:
                if st_info is None:
                    child.error = True
                else:
                    size = st_info.st_size
                    ino = getattr(st_info, "st_ino", 0)
                    nlink = getattr(st_info, "st_nlink", 1)
                    if ino and nlink > 1:
                        key = (getattr(st_info, "st_dev", 0), ino)
                        with inode_lock:
                            if key in seen_inodes:
                                child.hardlink_dup = True
                                size = 0
                            else:
                                seen_inodes.add(key)
                    child.size = size
                child.file_count = 1
                local_files += 1

        if local_files:
            with counter_lock:
                scanned[0] += local_files
                count = scanned[0]
            progress_q.put(("progress", count))

    def _worker():
        while True:
            try:
                node = work.get()
            except Exception:
                logger.debug("Scan worker queue.get() failed, exiting", exc_info=True)
                return
            try:
                _scan_one(node)
            finally:
                work.task_done()

    n = workers or _worker_count()
    threads = [
        threading.Thread(target=_worker, daemon=True) for _ in range(n)
    ]
    for t in threads:
        t.start()
    work.join()  # block until every queued directory has been processed

    # Roll sizes/counts up the tree (iterative post-order; deep trees safe).
    _rollup(root)
    progress_q.put(("progress", scanned[0]))
    return root


def _rollup(root):
    """Sum child sizes/file counts into each directory, bottom-up."""
    stack = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if not node.is_dir:
            continue
        if processed:
            for child in node.children:
                node.size += child.size
                node.file_count += child.file_count
        else:
            stack.append((node, True))
            for child in node.children:
                if child.is_dir:
                    stack.append((child, False))
