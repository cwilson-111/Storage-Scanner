"""Live progress for a running scan: the messages the scan engines post on
their progress_q, and the running totals the Compatible engine keeps while
it walks.

Every message is a ``(kind, payload)`` tuple:

``("live_tree", WalkTracker)``
    The Compatible engine (scanner.scan), once, before it reads anything:
    the tracker of the Node tree its workers build in place. The main
    window reads every folder's running totals and state through it
    (WalkTracker.folders), so a folder row can fill in while it's read.
``("walk", WalkSnapshot)``
    The Compatible engine: running totals for the whole walk and the
    folder that has been read the longest right now. Posted by a reporter
    thread every REPORT_INTERVAL_SECONDS and once more when the walk ends
    -- never per directory, so its cost doesn't grow with the number of
    folders.
``("phase", Phase)``
    A named step, with a done/total count when its size is known: reading
    the MFT, refreshing the Turbo Scan cache, adding up folder sizes.
``("estimate", (model, ScanEstimate or None))``
    How much work to expect, posted by the GUI itself (tagged with the
    scan's model, see ui/scan_progress_panel.py); storage_scanner.
    scan_progress_model turns these into the overall progress line.

Standard library only: the headless elevated helpers import this too.
"""

import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

QUEUED = "queued"
SCANNING = "scanning"
DONE = "done"

# How often a scan posts progress -- the UI polls at the same rate, so
# posting faster would only queue updates nobody sees.
REPORT_INTERVAL_SECONDS = 0.1

# scanner._scan_one reports a directory's counts every this many entries
# while it is still reading it. Without this a single huge folder (C:\
# Windows\WinSxS\Manifests: 35,822 files, ~18 s to list and stat) added
# nothing to the totals until it finished, and the scan looked stuck.
FLUSH_EVERY_ENTRIES = 1000


@dataclass(frozen=True)
class Phase:
    """One named step of a scan. total=None: the step's size is unknown."""

    label: str
    done: Optional[int] = None
    total: Optional[int] = None
    unit: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(
            label=str(data["label"]),
            done=data.get("done"),
            total=data.get("total"),
            unit=str(data.get("unit", "")),
        )


@dataclass(frozen=True)
class WalkSnapshot:
    files: int
    bytes: int  # logical, like the Size column
    alloc_bytes: int  # on disk, like the On Disk column
    folders: int  # directories fully read
    pending: int  # directories queued or being read
    current_path: Optional[str]  # the directory being read the longest
    current_seconds: float
    current_entries: int  # entries of current_path reported so far


class Throttle:
    """due() is True at most once per `interval` seconds, unless forced."""

    def __init__(self, interval, clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._last = None

    def due(self, force=False):
        now = self._clock()
        if force or self._last is None or now - self._last >= self._interval:
            self._last = now
            return True
        return False


class WalkTracker:
    """Thread-safe running totals for one Compatible scan: for the whole
    walk, and for every folder of the tree it builds.

    Each worker thread calls begin() when it starts reading a directory and
    record() when it has counts to add -- every FLUSH_EVERY_ENTRIES entries
    and once when the directory is done. record() adds the counts to the
    directory's own Node and to every folder above it, so at any moment a
    folder's size, alloc_size and file_count are what has been read under
    it so far. It takes the lock once per call: a directory costs one lock
    round trip (plus one per 1,000 entries) and one pass up its chain of
    ancestors, whatever depth anyone is looking at.

    A folder is done once every directory under it has been read. _pending
    counts, per folder, the directories in its subtree not fully read yet,
    and drops a folder when that reaches zero, so it only ever holds the
    folders still being worked on. A subdirectory has to be counted before
    it is queued -- record() does that under the lock, and the caller
    queues it afterwards -- or another worker could finish it first and
    briefly mark every folder above it done.

    These totals are a live view only: scanner._rollup() recomputes every
    folder's totals from its file rows once the walk ends, so a finished
    tree's numbers never depend on them.
    """

    def __init__(self, root, workers, clock=time.monotonic):
        self.root = root
        self._lock = threading.Lock()
        self._clock = clock
        self._files = 0
        self._bytes = 0
        self._alloc = 0
        self._folders = 0
        self._pending = {root: 1}  # folder -> unread directories in its subtree
        # Per worker: [node, started_at, entries] of the directory it's reading.
        self._active = [None] * workers

    def begin(self, worker, node):
        self._active[worker] = [node, self._clock(), 0]

    def record(self, worker, node, chain, files, nbytes, nalloc, entries, new_dirs, finished):
        """Add counts for `node`, the directory `worker` is reading;
        `chain` is every folder above it, the scan root first. `new_dirs`
        are subdirectories found since the last call, about to be queued."""
        change = len(new_dirs) - (1 if finished else 0)
        with self._lock:
            self._files += files
            self._bytes += nbytes
            self._alloc += nalloc
            if finished:
                self._folders += 1
            pending = self._pending
            if files or change:
                for folder in (*chain, node):
                    if files:
                        folder.size += nbytes
                        folder.alloc_size += nalloc
                        folder.file_count += files
                    if change:
                        left = pending[folder] + change
                        if left:
                            pending[folder] = left
                        else:
                            del pending[folder]
            for child in new_dirs:
                pending[child] = 1
        if finished:
            self._active[worker] = None
        else:
            self._active[worker][2] += entries

    def folders(self, nodes):
        """(size, alloc_size, file_count, state) for each folder in
        `nodes`, read together under the lock so a row never mixes two
        moments. A folder is QUEUED until a worker starts reading it,
        SCANNING until its whole subtree has been read, then DONE."""
        active = {id(entry[0]) for entry in list(self._active) if entry is not None}
        with self._lock:
            pending = self._pending
            return [
                (
                    node.size,
                    node.alloc_size,
                    node.file_count,
                    _state(node, pending, active),
                )
                for node in nodes
            ]

    def snapshot(self):
        now = self._clock()
        with self._lock:
            files, nbytes, alloc = self._files, self._bytes, self._alloc
            folders, pending = self._folders, self._pending.get(self.root, 0)
        longest = None
        for active in list(self._active):
            if active is not None and (longest is None or active[1] < longest[1]):
                longest = active
        if longest is None:
            current_path, current_seconds, current_entries = None, 0.0, 0
        else:
            node, started_at, current_entries = longest
            current_path = node.path
            current_seconds = max(0.0, now - started_at)
        return WalkSnapshot(
            files=files,
            bytes=nbytes,
            alloc_bytes=alloc,
            folders=folders,
            pending=pending,
            current_path=current_path,
            current_seconds=current_seconds,
            current_entries=current_entries,
        )


def _state(node, pending, active):
    if node not in pending:
        return DONE
    if node.dirs or node.file_names or id(node) in active:
        return SCANNING
    return QUEUED
