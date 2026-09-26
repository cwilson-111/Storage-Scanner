"""What the scan progress panel shows, worked out from a scan's progress
messages (see storage_scanner.scan_progress). No Tk here, so it can be
tested directly; storage_scanner/ui/scan_progress_panel.py only puts a
ProgressView on screen.

The overall bar needs to know how much work to expect, which a directory
walk can't know up front. load_estimate() looks it up before the scan:
the previous scan of the same path from scan history (file count, total
size, and the file count of each top-level folder it kept), else the drive's
used space when the target is a volume root. With neither, the bar
animates instead and the live counters carry the progress. Turbo Scan's
steps bring their own done/total counts (MFT records, journal changes).
"""

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional

import history
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.scan_history import normalize_scan_path
from storage_scanner.scan_progress import DONE, QUEUED, SCANNING

RUNNING = "running"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
FINISHED = "finished"
FAILED = "failed"

ESTIMATE_LAST_SCAN = "last scan"
ESTIMATE_DRIVE_USED = "drive used space"

# A running scan's bar never fills on an estimate alone: the folder may
# have grown since the last scan, and a full bar that keeps going looks
# broken.
RUNNING_FRACTION_CAP = 0.99

# How many of the last scan's biggest folders load_estimate reads to find
# the target's direct children. History keeps only folders of 50 MB and
# up; read biggest first, a child that doesn't make this cut gets a live bar.
_PRIOR_FOLDER_ROWS = 5000

_CURRENT_PATH_CHARS = 110
# A folder being read this long gets its time and item count shown, so a
# slow folder reads as busy rather than stuck.
_SLOW_FOLDER_SECONDS = 2.0
# Rates over less time than this are mostly noise.
_MIN_RATE_SECONDS = 1.0

_STATE_TEXT = {QUEUED: "queued", SCANNING: "scanning", DONE: "done"}
_STATE_ORDER = {SCANNING: 0, QUEUED: 1, DONE: 2}
_MORE_KEY = "more"


@dataclass(frozen=True)
class ScanEstimate:
    total_bytes: Optional[int]
    total_files: Optional[int]
    source: str  # ESTIMATE_LAST_SCAN | ESTIMATE_DRIVE_USED
    taken_at: Optional[str] = None  # the last scan's created_at
    # os.path.normcase(top-level folder name) -> its file count in the last scan
    folder_files: dict = field(default_factory=dict)


def choose_estimate(target, last_scan, folder_rows, volume_used_bytes):
    """The best estimate available for scanning `target`, or None.

    `last_scan` is history.get_latest_scan_snapshot()'s row (created_at,
    total_size, file_count, folder_count) or None; `folder_rows` are that
    scan's (folder_path, file_count) pairs; `volume_used_bytes` is the
    drive's used space when `target` is a volume root, else None. The last
    scan wins: it counted what this scanner counts, where used space also
    includes what a scan can't see (the page file, unreadable folders).
    """
    if last_scan is not None:
        taken_at, total_size, file_count = last_scan[0], last_scan[1], last_scan[2]
        if total_size > 0 or file_count > 0:
            return ScanEstimate(
                total_bytes=total_size or None,
                total_files=file_count or None,
                source=ESTIMATE_LAST_SCAN,
                taken_at=taken_at,
                folder_files=_direct_children(target, folder_rows),
            )
    if volume_used_bytes:
        return ScanEstimate(
            total_bytes=volume_used_bytes, total_files=None, source=ESTIMATE_DRIVE_USED
        )
    return None


def _direct_children(target, folder_rows):
    parent = normalize_scan_path(target)
    counts = {}
    for folder_path, file_count in folder_rows:
        folder = os.path.normpath(folder_path)
        name = os.path.basename(folder)
        if name and normalize_scan_path(os.path.dirname(folder)) == parent:
            counts[os.path.normcase(name)] = file_count
    return counts


def load_estimate(target):
    """choose_estimate() with its inputs read from scan history and the
    drive. Never raises -- an estimate is a nicety, not a reason to fail a
    scan. Reads the history database, so call it off the UI thread."""
    scan_path = normalize_scan_path(target)
    last_scan, folder_rows = None, []
    try:
        last_scan = history.get_latest_scan_snapshot(scan_path)
        if last_scan is not None:
            scan_id = history.get_latest_scan_id(scan_path)
            # Against no previous scan, "growth" rows are just this scan's
            # folders with their sizes and file counts -- the one history
            # reader that lists them.
            rows = history.get_folder_growth(scan_id, None, limit=_PRIOR_FOLDER_ROWS)
            folder_rows = [(row[0], row[6]) for row in rows]
    except Exception:  # noqa: BLE001 - an estimate is optional
        logger.warning("Could not read scan history to estimate %r", target, exc_info=True)
        last_scan, folder_rows = None, []

    used = None
    if os.path.ismount(target):
        try:
            used = shutil.disk_usage(target).used
        except OSError:
            logger.debug("disk_usage(%r) failed", target, exc_info=True)
    return choose_estimate(target, last_scan, folder_rows, used)


@dataclass(frozen=True)
class FolderRow:
    key: str  # stable across updates, for the panel's row identity
    name: str
    state: str
    size: str
    files: str
    fill: float  # how full the row's bar is, 0..1


@dataclass(frozen=True)
class ProgressView:
    headline: str
    fraction: Optional[float]  # None: amount of work unknown, animate
    percent: str
    counters: str
    estimate: str
    current: str
    folders_summary: str
    folders: tuple  # FolderRow, scanning first, then queued, then done


def _clamp(value, low, high):
    return max(low, min(high, value))


def _format_elapsed(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d} elapsed"
    return f"{minutes}:{secs:02d} elapsed"


def _rate(count, seconds):
    return count / seconds if seconds >= _MIN_RATE_SECONDS else None


def _shorten(path, limit=_CURRENT_PATH_CHARS):
    return path if len(path) <= limit else "…" + path[-(limit - 1) :]


class ScanProgressModel:
    """One scan's progress, fed its progress_q messages by the UI thread.

    handle() only stores the latest message of each kind, so a burst of
    messages costs almost nothing; view() does the formatting, once per
    panel refresh.
    """

    def __init__(self, target, clock=time.monotonic):
        self.target = target
        self._clock = clock
        self._started_at = clock()
        self.outcome = RUNNING
        self.estimate = None
        self.estimate_known = False
        self.walk = None  # latest WalkSnapshot
        self.phase = None  # latest Phase; None while the walk is the current step
        self._walk_started_at = None
        self._phase_started_at = None
        self._frozen_fraction = None

    def handle(self, kind, payload):
        """Apply one progress message; False for a kind this model doesn't
        use. After cancel or finish the display is frozen, so late
        messages from the scan thread are ignored."""
        if kind == "estimate":
            self.estimate = payload
            self.estimate_known = True
        elif kind == "walk":
            if self.outcome == RUNNING:
                if self._walk_started_at is None:
                    # A Turbo Scan that fell back already spent time on its
                    # own steps; the walk's rate should only count the walk.
                    self._walk_started_at = (
                        self._started_at if self.phase is None else self._clock()
                    )
                self.walk = payload
                self.phase = None
        elif kind == "phase":
            if self.outcome == RUNNING:
                if self.phase is None or self.phase.label != payload.label:
                    self._phase_started_at = self._clock()
                self.phase = payload
        else:
            return False
        return True

    def request_cancel(self):
        if self.outcome == RUNNING:
            self._frozen_fraction = self.fraction()
            self.outcome = CANCELLING

    def finish(self, outcome):
        """End the scan as FINISHED, CANCELLED or FAILED."""
        if outcome != FINISHED and self.outcome == RUNNING:
            self._frozen_fraction = self.fraction()
        self.outcome = outcome

    def fraction(self):
        """How far along the scan is, 0..1, or None when that isn't known."""
        if self.outcome == FINISHED:
            return 1.0
        if self.outcome != RUNNING:
            return self._frozen_fraction
        if self.phase is not None:
            phase = self.phase
            if phase.total and phase.done is not None:
                return _clamp(phase.done / phase.total, 0.0, 1.0)
            return None
        if self.walk is None or self.estimate is None:
            return None
        if self.estimate.total_files:
            # Files, not bytes, when known: time goes on per-file work, and
            # one huge file would otherwise jump the bar.
            ratio = self.walk.files / self.estimate.total_files
        elif self.estimate.total_bytes:
            ratio = self.walk.bytes / self.estimate.total_bytes
        else:
            return None
        return _clamp(ratio, 0.0, RUNNING_FRACTION_CAP)

    def view(self):
        now = self._clock()
        fraction = self.fraction()
        rows, summary = self._folder_rows()
        return ProgressView(
            headline=self._headline(),
            fraction=fraction,
            percent="" if fraction is None else f"{int(fraction * 100)}%",
            counters=self._counters(now),
            estimate=self._estimate_text(),
            current=self._current_text(),
            folders_summary=summary,
            folders=rows,
        )

    def _counted_phase(self):
        phase = self.phase
        return phase if phase is not None and phase.total else None

    def _headline(self):
        if self.outcome == CANCELLING:
            return "Cancelling…"
        if self.outcome == CANCELLED:
            return "Scan cancelled"
        if self.outcome == FAILED:
            return "Scan failed"
        if self.outcome == FINISHED:
            return "Scan complete"
        if self.phase is not None:
            return f"{self.phase.label}…"
        return f"Scanning {self.target}…"

    def _counters(self, now):
        parts = []
        phase = self._counted_phase()
        if phase is not None:
            done = phase.done or 0
            parts.append(f"{done:,} of {phase.total:,} {phase.unit}".rstrip())
            rate = _rate(done, now - self._phase_started_at)
            if rate is not None and phase.unit:
                parts.append(f"{rate:,.0f} {phase.unit}/s")
        elif self.walk is not None:
            walk = self.walk
            parts += [f"{walk.files:,} files", f"{walk.folders:,} folders", human_size(walk.bytes)]
            rate = _rate(walk.files, now - self._walk_started_at)
            if rate is not None:
                parts.append(f"{rate:,.0f} files/s")
        parts.append(_format_elapsed(now - self._started_at))
        return " · ".join(parts)

    def _estimate_text(self):
        if self._counted_phase() is not None:
            return ""  # the step's own count is the estimate
        estimate = self.estimate
        if estimate is None:
            if self.estimate_known and self.walk is not None:
                return "No earlier scan of this folder to compare with — showing live counts"
            return ""
        amounts = []
        if estimate.total_files:
            amounts.append(f"{estimate.total_files:,} files")
        if estimate.total_bytes:
            amounts.append(human_size(estimate.total_bytes))
        if estimate.source == ESTIMATE_LAST_SCAN:
            when = f" ({estimate.taken_at[:10]})" if estimate.taken_at else ""
            origin = f"the last scan{when}"
        else:
            origin = "the drive's used space"
        return f"Expecting about {', '.join(amounts)}, going by {origin}"

    def _current_text(self):
        walk = self.walk
        if self.outcome != RUNNING or self.phase is not None or walk is None:
            return ""
        if not walk.current_path:
            return ""
        text = f"Now: {_shorten(walk.current_path)}"
        if walk.current_seconds >= _SLOW_FOLDER_SECONDS:
            text += f" — {walk.current_seconds:.0f} s in this folder"
            if walk.current_entries:
                text += f", {walk.current_entries:,} items so far"
        return text

    def _folder_rows(self):
        walk = self.walk
        if walk is None:
            return (), ""
        prior = self.estimate.folder_files if self.estimate is not None else {}
        largest = max((folder.files for folder in walk.top_folders), default=0)
        indexed = sorted(
            enumerate(walk.top_folders),
            key=lambda item: (
                _STATE_ORDER[item[1].state],
                -item[1].bytes if item[1].state == DONE else item[0],
            ),
        )
        rows = [
            self._row(
                str(index), folder.name, folder, prior.get(os.path.normcase(folder.name)), largest
            )
            for index, folder in indexed
        ]
        total = len(walk.top_folders)
        done = sum(1 for folder in walk.top_folders if folder.state == DONE)
        scanning = sum(1 for folder in walk.top_folders if folder.state == SCANNING)
        more = walk.more_folders
        if more is not None:
            rows.append(self._row(_MORE_KEY, f"+ {more.folders:,} more folders", more, None, 0))
            total += more.folders
            if more.state == DONE:
                done += more.folders
        if not total:
            return (), ""
        summary = f"Top-level folders: {done:,} of {total:,} done"
        if scanning:
            summary += f", {scanning:,} scanning"
        return tuple(rows), summary

    @staticmethod
    def _row(key, name, folder, prior_files, largest):
        """A folder's bar fills against its file count in the last scan
        when that's known -- files, like the overall bar, because time
        goes on per-file work and a few big files would fill a size-based
        bar long before the folder is done. Otherwise it fills against the
        top-level folder with the most files so far, so it still grows
        while being scanned. Either way it never fills before the folder
        is done."""
        if folder.state == DONE:
            fill = 1.0
        elif folder.state == QUEUED:
            fill = 0.0
        elif prior_files:
            fill = min(folder.files / prior_files, RUNNING_FRACTION_CAP)
        elif largest:
            fill = min(folder.files / largest, RUNNING_FRACTION_CAP)
        else:
            fill = 0.0
        queued = folder.state == QUEUED
        return FolderRow(
            key=key,
            name=name,
            state=_STATE_TEXT[folder.state],
            size="" if queued else human_size(folder.bytes),
            files="" if queued else f"{folder.files:,}",
            fill=fill,
        )
