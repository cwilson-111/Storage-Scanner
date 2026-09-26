"""What the scan progress line under the tree shows, worked out from a
scan's progress messages (see storage_scanner.scan_progress). No Tk here,
so it can be tested directly; storage_scanner/ui/scan_progress_panel.py
only puts a ProgressView on screen. The folders themselves fill in inside
the main tree (storage_scanner.live_tree_model).

The overall bar needs to know how much work to expect, which a directory
walk can't know up front. load_estimate() looks it up before the scan:
the file count of the previous scan of the same path, from scan history,
else the drive's used space when the target is a volume root. With
neither, the bar animates instead and the live counters carry the
progress. Turbo Scan's steps bring their own done/total counts (MFT
records, journal changes).
"""

import os
import shutil
import time
from dataclasses import dataclass
from typing import Optional

import history
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.scan_history import normalize_scan_path

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

# The "Now:" folder is shortened to this many characters, keeping its end.
_CURRENT_PATH_CHARS = 60
# A folder being read this long gets its time and item count shown, so a
# slow folder reads as busy rather than stuck.
_SLOW_FOLDER_SECONDS = 2.0
# Rates over less time than this are mostly noise.
_MIN_RATE_SECONDS = 1.0


@dataclass(frozen=True)
class ScanEstimate:
    """How much work a scan should expect: the last scan's file count, or
    the drive's used (on-disk) bytes. Never the last scan's total size: that
    is the sum of logical file sizes, which sparse and compressed files and
    online-only cloud files push past what's on disk -- C:\\ once "expected"
    2.1 TB on a 1.9 TB drive, 0.5 TB of it one sparse emulator disk image."""

    total_files: Optional[int]
    disk_bytes: Optional[int]
    source: str  # ESTIMATE_LAST_SCAN | ESTIMATE_DRIVE_USED
    taken_at: Optional[str] = None  # the last scan's created_at


def choose_estimate(last_scan, volume_used_bytes):
    """The best estimate available, or None.

    `last_scan` is history.get_latest_scan_snapshot()'s row (created_at,
    total_size, file_count, folder_count) for the target, or None;
    `volume_used_bytes` is the drive's used space when the target is a
    volume root, else None. The last scan wins: it counted what this scanner
    counts, where used space also includes what a scan can't see (the page
    file, unreadable folders, NTFS's own metadata).
    """
    if last_scan is not None and last_scan[2] > 0:
        return ScanEstimate(
            total_files=last_scan[2],
            disk_bytes=None,
            source=ESTIMATE_LAST_SCAN,
            taken_at=last_scan[0],
        )
    if volume_used_bytes:
        return ScanEstimate(
            total_files=None, disk_bytes=volume_used_bytes, source=ESTIMATE_DRIVE_USED
        )
    return None


def load_estimate(target):
    """choose_estimate() with its inputs read from scan history and the
    drive. Never raises -- an estimate is a nicety, not a reason to fail a
    scan. Reads the history database, so call it off the UI thread."""
    last_scan = None
    try:
        last_scan = history.get_latest_scan_snapshot(normalize_scan_path(target))
    except Exception:  # noqa: BLE001 - an estimate is optional
        logger.warning("Could not read scan history to estimate %r", target, exc_info=True)

    used = None
    if os.path.ismount(target):
        try:
            used = shutil.disk_usage(target).used
        except OSError:
            logger.debug("disk_usage(%r) failed", target, exc_info=True)
    return choose_estimate(last_scan, used)


@dataclass(frozen=True)
class ProgressView:
    headline: str
    fraction: Optional[float]  # None: amount of work unknown, animate
    percent: str
    counters: str
    estimate: str
    current: str
    # The step under way when there's no folder tree to fill in yet (Turbo
    # Scan, an elevated helper), for the tree's root row; "" otherwise.
    step: str


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
    refresh.
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
            # Files when known: time goes on per-file work, and one huge
            # file would otherwise jump the bar.
            ratio = self.walk.files / self.estimate.total_files
        elif self.estimate.disk_bytes:
            # On disk against on disk: a sparse file's logical size can be
            # hundreds of times what it uses.
            ratio = self.walk.alloc_bytes / self.estimate.disk_bytes
        else:
            return None
        return _clamp(ratio, 0.0, RUNNING_FRACTION_CAP)

    def view(self):
        now = self._clock()
        fraction = self.fraction()
        return ProgressView(
            headline=self._headline(),
            fraction=fraction,
            percent="" if fraction is None else f"{int(fraction * 100)}%",
            counters=self._counters(now),
            estimate=self._estimate_text(),
            current=self._current_text(),
            step=self._step_text(fraction),
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
        return "Scanning…"

    def _step_text(self, fraction):
        if self.outcome != RUNNING or self.phase is None:
            return ""
        if self._counted_phase() is not None and fraction is not None:
            return f"{self.phase.label} — {int(fraction * 100)}%"
        return f"{self.phase.label}…"

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
                return "No earlier scan to compare with"
            return ""
        if estimate.source == ESTIMATE_LAST_SCAN:
            when = f", {estimate.taken_at[:10]}" if estimate.taken_at else ""
            return f"Expecting about {estimate.total_files:,} files (last scan{when})"
        return f"Expecting about {human_size(estimate.disk_bytes)} on disk (drive's used space)"

    def _current_text(self):
        walk = self.walk
        if self.outcome != RUNNING or self.phase is not None or walk is None:
            return ""
        if not walk.current_path:
            return ""
        text = f"Now: {_shorten(walk.current_path)}"
        if walk.current_seconds >= _SLOW_FOLDER_SECONDS:
            text += f" — {walk.current_seconds:.0f} s"
            if walk.current_entries:
                text += f", {walk.current_entries:,} items"
        return text
