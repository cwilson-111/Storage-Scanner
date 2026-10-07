#!/usr/bin/env python3
"""Compare Turbo Scan's output against the Compatible engine's for the
same path(s) -- the release-blocking validation gate from the Turbo Scan
plan (NEURAL_STORAGE_MATRIX_PROJECT_ROADMAP.md). Run this across a range
of real targets (a small folder, a huge one, one with known hard links or
junctions, one with OneDrive placeholders, one with NTFS-compressed files)
before ever recommending or defaulting Turbo Scan on.

Must be run from an elevated ("Run as Administrator") terminal: Turbo
Scan's in-process path needs admin rights to read the raw volume, and
this tool deliberately never triggers its own UAC prompt mid-run -- it's
meant to be scriptable and repeatable, not something to babysit.
turbo_checklist.py (P1-3) runs the same comparison as steps of its
checklist, through compare_path() and classify().

Usage:
    python compare_scan_engines.py <path> [<path> ...]

Exit codes:
    0 - every path matched, or every difference has a known explanation
        (or no path was NTFS-eligible, so there was nothing to compare)
    1 - not elevated, not Windows, or bad arguments
    2 - at least one path showed a difference nothing explains

A live folder changes while two engines read it one after the other, and
some differences are expected, so each one is classified (classify()):

- hard-link order: for a file with more than one hard link, the engines
  can disagree about *which* occurrence is the counted one and which the
  zero-sized "hardlink_dup" -- each finds the links in a different order --
  while the file's total contribution matches either way (a hard-link-heavy
  target such as C:\\Windows\\WinSxS shows many);
- changed during the run: the path is gone now, or it (or, for an added or
  missing entry, its folder) was modified after the run started, or its
  size now differs from what the Compatible engine read;
- unreadable to Compatible: the Compatible engine couldn't list the folder
  or read the file (access denied even elevated), which Turbo Scan reads
  from the MFT anyway;
- small file inside its MFT record: a file under 4 KiB can live inside its
  MFT record, with no cluster of its own -- Turbo Scan bills its length,
  while the Compatible engine can't tell and rounds it up to a whole
  cluster (alloc_size._windows_alloc_size);
- allocated past its end: NTFS has more allocated to the file than its
  length rounded to a cluster (room reserved for a file being written, or
  preallocated, as ETW logs are); Turbo Scan bills that allocation and the
  Compatible engine the rounded length, and the file's allocation right now
  equals Turbo Scan's (live_file_state);
- open for writing: something holds the file open for writing now, so its
  size is a moving target and its MFT record on disk lags what the file
  system reports -- its size, and even its name after a rename (a database
  log renamed into place shows as missing from Turbo Scan) -- even when its
  modified time is older than the run;
- folder total of the above: a folder's size, on-disk size or file count
  differs only because something below it does.

Anything else is "unexplained": a real Turbo Scan bug until shown otherwise.
"""

import argparse
import os
import queue
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

from live_file_state import live_file_state
from storage_scanner.models import iter_folders
from storage_scanner.platform_support import IS_ROOT, IS_WINDOWS
from storage_scanner.scanner import scan as compatible_scan
from storage_scanner.turbo_scan import ENGINE_TURBO, scan_with_best_engine

_COMPARED_FIELDS = (
    "size",
    "alloc_size",
    "file_count",
    "is_dir",
    "is_link",
    "is_cloud_placeholder",
    "hardlink_dup",
)
# A folder's own totals: a difference there only repeats one found below it.
_TOTAL_FIELDS = ("size", "alloc_size", "file_count")

MISSING = "missing from Turbo"
EXTRA = "extra in Turbo"
MISMATCH = "mismatch"

HARDLINK_ORDER = "hard-link order"
CHANGED = "changed during the run"
UNREADABLE = "unreadable to Compatible"
RESIDENT = "small file inside its MFT record"
PREALLOCATED = "allocated past its end"
OPEN_FOR_WRITING = "open for writing"
ROLLUP = "folder total of the above"
UNEXPLAINED = "unexplained"

# How long before the run started a modification still counts as during it:
# NTFS and the clock read at the start don't tick together.
_CLOCK_SLACK_SECONDS = 2.0
# The largest MFT record is 4 KiB: a file that size or bigger can't fit in one.
_MAX_RESIDENT_SIZE = 4096


@dataclass(frozen=True)
class Discrepancy:
    """One difference between the two trees: a path only one engine found
    (MISSING, EXTRA), or one field of a path both found (MISMATCH)."""

    kind: str
    path: str
    field: Optional[str] = None
    compatible: Any = None
    turbo: Any = None

    def __str__(self):
        if self.kind == MISSING:
            return f"MISSING FROM TURBO: {self.path}"
        if self.kind == EXTRA:
            return f"EXTRA IN TURBO: {self.path}"
        return (
            f"{self.field} MISMATCH at {self.path}: "
            f"compatible={self.compatible!r} turbo={self.turbo!r}"
        )


def _flatten(node, out=None):
    """path -> node, for every node in the tree."""
    if out is None:
        out = {}
    out[node.path] = node
    for child in node.children:
        _flatten(child, out)
    return out


def _long_path(path):
    """`path` with 8.3 short names (DANET~1) expanded, links not followed."""
    import ctypes

    get_long = ctypes.windll.kernel32.GetLongPathNameW
    size = get_long(path, None, 0)
    if not size:
        return path
    buffer = ctypes.create_unicode_buffer(size)
    return buffer.value if get_long(path, buffer, size) else path


def on_disk_spelling(path):
    """`path` as NTFS stores it: long names, each part in its own case, the
    drive letter upper-case (c:\\WINDOWS -> C:\\Windows). Turbo Scan names
    its tree that way whatever was asked, so the Compatible engine has to be
    asked the same, or every path differs. Unlike os.path.realpath, a
    junction or symlink stays itself (asking for one directly is a case
    to compare). Unchanged off Windows, or where a part can't be listed."""
    if not IS_WINDOWS:
        return path
    path = _long_path(os.path.abspath(path))
    drive, rest = os.path.splitdrive(path)
    spelled = (drive if drive.startswith("\\\\") else drive.upper()) + os.sep
    for part in rest.split(os.sep):
        if not part:
            continue
        try:
            names = os.listdir(spelled)
        except OSError:
            return path
        if part not in names:
            wanted = part.casefold()
            part = next((name for name in names if name.casefold() == wanted), None)
            if part is None:
                return path
        spelled = os.path.join(spelled, part)
    return spelled


def _compare(compatible_root, turbo_root):
    """Every Discrepancy between the two trees, empty if they agree on
    every compared field for every node."""
    compat_by_path = _flatten(compatible_root)
    turbo_by_path = _flatten(turbo_root)

    discrepancies = [
        Discrepancy(MISSING, p) for p in sorted(set(compat_by_path) - set(turbo_by_path))
    ]
    discrepancies += [
        Discrepancy(EXTRA, p) for p in sorted(set(turbo_by_path) - set(compat_by_path))
    ]

    for path in sorted(set(compat_by_path) & set(turbo_by_path)):
        c, t = compat_by_path[path], turbo_by_path[path]
        for field in _COMPARED_FIELDS:
            c_val, t_val = getattr(c, field), getattr(t, field)
            if c_val != t_val:
                discrepancies.append(Discrepancy(MISMATCH, path, field, c_val, t_val))

    return discrepancies


def _stat_now(path):
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError:
        return None


def _unreadable_paths(compatible_root):
    """Where the Compatible engine's own reads failed: every file it couldn't
    stat, and every folder it couldn't (fully) list. A folder's error flag is
    also set on all its ancestors (scanner._rollup), so only a flagged folder
    with no flagged file or subfolder of its own counts as a failed listing."""
    found = set()
    for folder in iter_folders(compatible_root):
        bad_files = [f.path for f in folder.files() if f.error]
        found.update(bad_files)
        if folder.error and not bad_files and not any(d.error for d in folder.dirs):
            found.add(folder.path)
    return found


def _ancestors(path):
    parent = os.path.dirname(path)
    while parent and parent != path:
        yield parent
        path, parent = parent, os.path.dirname(parent)


def _changed_since(discrepancy, compat_node, now, since, stat_now):
    if now is None:
        return True  # deleted, or created and deleted again, during the run
    if now.st_mtime >= since - _CLOCK_SLACK_SECONDS:
        return True
    if (
        compat_node is not None
        and not compat_node.is_dir
        and not compat_node.hardlink_dup
        and now.st_size != compat_node.size
    ):
        return True  # grew or shrank after the Compatible engine read it
    if discrepancy.kind in (MISSING, EXTRA):
        # Created, deleted or renamed: the folder holding it was modified.
        folder = stat_now(os.path.dirname(discrepancy.path))
        return folder is None or folder.st_mtime >= since - _CLOCK_SLACK_SECONDS
    return False


def _hardlink_order(discrepancy, compat_node, turbo_node, now):
    """The two engines picked different occurrences of a hard-linked file as
    the counted one: the flags disagree, the file really has several links,
    and only the flag or the sizes it zeroes differ."""
    if compat_node is None or turbo_node is None or compat_node.is_dir or turbo_node.is_dir:
        return False
    if compat_node.hardlink_dup == turbo_node.hardlink_dup:
        return False
    if now is not None and now.st_nlink < 2:
        return False
    return discrepancy.field in ("hardlink_dup", "size", "alloc_size")


def _resident(discrepancy, compat_node, turbo_node):
    """A file small enough to live inside its MFT record: Turbo Scan bills
    its length as its on-disk size, the Compatible engine a whole cluster."""
    if discrepancy.field != "alloc_size" or compat_node is None or turbo_node is None:
        return False
    size = compat_node.size
    return (
        not compat_node.is_dir
        and 0 < size < _MAX_RESIDENT_SIZE
        and turbo_node.size == turbo_node.alloc_size == size
        and compat_node.alloc_size > size
        and compat_node.alloc_size % 512 == 0  # a whole number of clusters
    )


def _file_mismatch(discrepancy, compat_node, turbo_node):
    return (
        discrepancy.kind == MISMATCH
        and compat_node is not None
        and turbo_node is not None
        and not compat_node.is_dir
        and not turbo_node.is_dir
    )


def _preallocated(discrepancy, compat_node, turbo_node, live):
    """NTFS has the very allocation Turbo Scan billed, more than the
    Compatible engine's cluster-rounded length."""
    return (
        _file_mismatch(discrepancy, compat_node, turbo_node)
        and discrepancy.field == "alloc_size"
        and live.allocation is not None
        and turbo_node.alloc_size == live.allocation > compat_node.alloc_size
    )


def _open_for_writing(discrepancy, compat_node, turbo_node, live):
    if not live.open_for_writing:
        return False
    if discrepancy.kind == MISSING:
        return compat_node is not None and not compat_node.is_dir
    return _file_mismatch(discrepancy, compat_node, turbo_node) and discrepancy.field in (
        "size",
        "alloc_size",
    )


def classify(
    discrepancies, compatible_root, turbo_root, since=None, stat_now=_stat_now, live=live_file_state
):
    """[(Discrepancy, category)] for every discrepancy, the category one of
    HARDLINK_ORDER, CHANGED, UNREADABLE, RESIDENT, PREALLOCATED,
    OPEN_FOR_WRITING, ROLLUP or UNEXPLAINED (see the module docstring).
    `since` is when the run started (time.time()); None means nothing should
    have changed, so neither CHANGED nor OPEN_FOR_WRITING is used -- for a
    folder only the caller writes to. `stat_now(path)` is the path's
    os.stat_result now (not following links), or None if it's gone;
    `live(path)` its live_file_state.LiveState."""
    if not discrepancies:
        return []
    compat_by_path = _flatten(compatible_root)
    turbo_by_path = _flatten(turbo_root)
    unreadable = _unreadable_paths(compatible_root)

    leaves, totals = [], []
    for d in discrepancies:
        c, t = compat_by_path.get(d.path), turbo_by_path.get(d.path)
        if d.field in _TOTAL_FIELDS and c is not None and c.is_dir and t is not None and t.is_dir:
            totals.append(d)
        else:
            leaves.append(d)

    classified = []
    below_a_leaf = set()  # every folder above a leaf discrepancy
    for d in leaves:
        c, t = compat_by_path.get(d.path), turbo_by_path.get(d.path)
        if d.path in unreadable or any(a in unreadable for a in _ancestors(d.path)):
            category = UNREADABLE
        else:
            now = stat_now(d.path)
            if since is not None and _changed_since(d, c, now, since, stat_now):
                category = CHANGED
            elif _hardlink_order(d, c, t, now):
                category = HARDLINK_ORDER
            elif _resident(d, c, t):
                category = RESIDENT
            elif _preallocated(d, c, t, state := live(d.path)):
                category = PREALLOCATED
            elif since is not None and _open_for_writing(d, c, t, state):
                category = OPEN_FOR_WRITING
            else:
                category = UNEXPLAINED
        classified.append((d, category))
        for ancestor in _ancestors(d.path):
            if ancestor in below_a_leaf:
                break  # and so are all of its own ancestors
            below_a_leaf.add(ancestor)

    for d in totals:
        classified.append((d, ROLLUP if d.path in below_a_leaf else UNEXPLAINED))
    return classified


@dataclass
class Comparison:
    """Both engines' trees for one path, and how they differ. `turbo_root`
    is None when Turbo Scan didn't run (report.fallback_reason says why)."""

    path: str
    compatible_root: Any
    compatible_seconds: float
    turbo_root: Any
    report: Any
    classified: list

    @property
    def turbo_ran(self):
        return self.report.engine == ENGINE_TURBO

    def counts(self):
        """category -> number of discrepancies."""
        return Counter(category for _d, category in self.classified)

    def unexplained(self):
        return [d for d, category in self.classified if category == UNEXPLAINED]


def compare_path(path, since=None):
    """Run the Compatible engine, then Turbo Scan (through the same
    scan_with_best_engine the app uses), on `path` (in its on-disk
    spelling) and classify how they differ. `since` as for classify()."""
    path = on_disk_spelling(path)
    progress_q, cancel_event = queue.Queue(), threading.Event()
    start = time.perf_counter()
    compatible_root = compatible_scan(path, progress_q, cancel_event)
    compatible_seconds = time.perf_counter() - start

    progress_q, cancel_event = queue.Queue(), threading.Event()
    turbo_root, report = scan_with_best_engine(path, progress_q, cancel_event, turbo_enabled=True)
    if report.engine != ENGINE_TURBO:
        return Comparison(path, compatible_root, compatible_seconds, None, report, [])
    classified = classify(_compare(compatible_root, turbo_root), compatible_root, turbo_root, since)
    return Comparison(path, compatible_root, compatible_seconds, turbo_root, report, classified)


def compare_one(path):
    """Print one path's comparison. True if every difference is explained,
    False if one isn't, None if Turbo Scan didn't run for it."""
    print(f"\n=== {path} ===")
    result = compare_path(path, since=time.time())
    compatible_root, report = result.compatible_root, result.report
    print(
        f"Compatible: {compatible_root.size:,} bytes, {compatible_root.file_count:,} files "
        f"in {result.compatible_seconds:.1f}s"
    )
    if not result.turbo_ran:
        print(
            f"SKIP: Turbo Scan did not actually run for this path "
            f"(fallback_reason={report.fallback_reason!r}) -- nothing to compare."
        )
        return None
    print(
        f"Turbo Scan: {result.turbo_root.size:,} bytes, {result.turbo_root.file_count:,} files "
        f"in {report.elapsed_seconds:.1f}s"
    )
    if report.elapsed_seconds > 0:
        print(f"  speedup: {result.compatible_seconds / report.elapsed_seconds:.1f}x")

    if not result.classified:
        print("MATCH: Turbo Scan agrees with the Compatible engine.")
        return True
    for category, count in sorted(result.counts().items()):
        print(f"  {count:,} {category}")
        if category != UNEXPLAINED:
            for d in [d for d, c in result.classified if c == category][:5]:
                print(f"      e.g. {d}")
    unexplained = result.unexplained()
    if not unexplained:
        print("EXPLAINED: every difference has a known cause.")
        return True
    print(f"MISMATCH ({len(unexplained)} unexplained):")
    for d in unexplained[:50]:
        print(f"  - {d}")
    if len(unexplained) > 50:
        print(f"  ... and {len(unexplained) - 50} more")
    return False


def main(argv):
    parser = argparse.ArgumentParser(
        description="Compare Turbo Scan's output against the Compatible engine's.",
    )
    parser.add_argument("paths", nargs="+", help="One or more folders/drives to compare")
    args = parser.parse_args(argv)

    if not IS_WINDOWS:
        print("This tool is Windows-only (Turbo Scan is NTFS-only).", file=sys.stderr)
        return 1
    if not IS_ROOT:
        print(
            'Must be run from an elevated ("Run as Administrator") terminal -- '
            "Turbo Scan's in-process path needs admin rights to read the raw "
            "volume, and this tool deliberately never triggers a UAC prompt "
            "mid-run.",
            file=sys.stderr,
        )
        return 1

    results = [compare_one(path) for path in args.paths]
    matched = results.count(True)
    mismatched = results.count(False)
    skipped = results.count(None)
    print(f"\n{'=' * 60}\n{matched} matched, {mismatched} mismatched, {skipped} skipped")

    return 2 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
