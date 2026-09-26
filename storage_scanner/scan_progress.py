"""Live progress for a running scan: the messages the scan engines post on
their progress_q, and the running totals the Compatible engine keeps while
it walks.

Every message is a ``(kind, payload)`` tuple:

``("walk", WalkSnapshot)``
    The Compatible engine (scanner.scan): running totals, the folder that
    has been read the longest right now, and per-top-level-folder
    accounting. Posted by a reporter thread every REPORT_INTERVAL_SECONDS
    and once more when the walk ends -- never per directory, so its cost
    doesn't grow with the number of folders.
``("phase", Phase)``
    A named step, with a done/total count when its size is known: reading
    the MFT, refreshing the Turbo Scan cache, adding up folder sizes.
``("estimate", (model, ScanEstimate or None))``
    How much work to expect, posted by the GUI itself (tagged with the
    scan's model, see ui/scan_progress_panel.py); storage_scanner.
    scan_progress_model turns all three into what the progress panel shows.

Standard library only: the headless elevated helpers import this too.
"""

import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

QUEUED = "queued"
SCANNING = "scanning"
DONE = "done"

# Top-level folders are tracked one by one up to this many; the rest share
# one aggregate entry, so a root holding tens of thousands of folders
# (C:\Windows\WinSxS) costs the same per snapshot as one holding a dozen.
MAX_TRACKED_FOLDERS = 200

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
class FolderProgress:
    """One top-level folder of the scan target, or (folders > 1) the
    aggregate of every top-level folder past MAX_TRACKED_FOLDERS."""

    name: str
    files: int
    bytes: int
    state: str  # QUEUED | SCANNING | DONE
    folders: int = 1


@dataclass(frozen=True)
class WalkSnapshot:
    files: int
    bytes: int
    folders: int  # directories fully read
    pending: int  # directories queued or being read
    current_path: Optional[str]  # the directory being read the longest
    current_seconds: float
    current_entries: int  # entries of current_path reported so far
    top_folders: tuple  # FolderProgress, in the order they were found
    more_folders: Optional[FolderProgress]


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


class _Slot:
    """Mutable running totals behind one FolderProgress."""

    __slots__ = ("name", "files", "bytes", "pending", "started", "folders")

    def __init__(self, name):
        self.name = name
        self.files = 0
        self.bytes = 0
        self.pending = 0  # this subtree's directories not yet fully read
        self.started = False
        self.folders = 0

    def progress(self):
        if self.pending == 0:
            state = DONE
        elif self.started:
            state = SCANNING
        else:
            state = QUEUED
        return FolderProgress(self.name, self.files, self.bytes, state, self.folders)


class WalkTracker:
    """Thread-safe running totals for one Compatible scan.

    Each worker thread calls begin() when it starts reading a directory and
    record() when it has counts to add -- every FLUSH_EVERY_ENTRIES entries
    and once when the directory is done. record() takes the lock once per
    call, so a directory costs one lock round trip (plus one per 1,000
    entries), about what the per-directory queue messages it replaced cost.

    A slot is the top-level folder a directory belongs to (None for the
    scan target itself). A slot is done once every directory under it has
    been read: its `pending` count goes up when a subdirectory is queued and
    down when a directory finishes. A subdirectory has to be counted before
    it is queued -- see record()'s return value -- or another worker could
    finish it first and briefly mark the whole folder done.
    """

    def __init__(self, workers, max_folders=MAX_TRACKED_FOLDERS, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._max_folders = max_folders
        self._files = 0
        self._bytes = 0
        self._folders = 0
        self._pending = 1  # the scan target itself
        self._slots = []
        self._more = None
        # Per worker: [path, started_at, entries] of the directory it's reading.
        self._active = [None] * workers

    def begin(self, worker, slot, path):
        if slot is not None:
            slot.started = True
        self._active[worker] = [path, self._clock(), 0]

    def record(self, worker, slot, files, nbytes, entries, new_dir_names, finished):
        """Add counts for the directory `worker` is reading. `new_dir_names`
        are subdirectories about to be queued. Returns the slot each of
        them belongs to, to queue alongside it: a new top-level slot when
        `slot` is None (the scan target's own listing), `slot` otherwise."""
        new_dirs = len(new_dir_names)
        pending_change = new_dirs - (1 if finished else 0)
        with self._lock:
            self._files += files
            self._bytes += nbytes
            self._pending += pending_change
            if finished:
                self._folders += 1
            if slot is None:
                slots = [self._register(name) for name in new_dir_names]
            else:
                slot.files += files
                slot.bytes += nbytes
                slot.pending += pending_change
                slots = [slot] * new_dirs
        if finished:
            self._active[worker] = None
        else:
            self._active[worker][2] += entries
        return slots

    def _register(self, name):
        if len(self._slots) < self._max_folders:
            slot = _Slot(name)
            self._slots.append(slot)
        else:
            if self._more is None:
                self._more = _Slot("")
            slot = self._more
        slot.folders += 1
        slot.pending += 1
        return slot

    def snapshot(self):
        now = self._clock()
        with self._lock:
            top_folders = tuple(slot.progress() for slot in self._slots)
            more_folders = self._more.progress() if self._more is not None else None
            files, nbytes = self._files, self._bytes
            folders, pending = self._folders, self._pending
        longest = None
        for active in list(self._active):
            if active is not None and (longest is None or active[1] < longest[1]):
                longest = active
        if longest is None:
            current_path, current_seconds, current_entries = None, 0.0, 0
        else:
            current_path, started_at, current_entries = longest
            current_seconds = max(0.0, now - started_at)
        return WalkSnapshot(
            files=files,
            bytes=nbytes,
            folders=folders,
            pending=pending,
            current_path=current_path,
            current_seconds=current_seconds,
            current_entries=current_entries,
            top_folders=top_folders,
            more_folders=more_folders,
        )
