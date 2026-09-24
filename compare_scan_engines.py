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

Usage:
    python compare_scan_engines.py <path> [<path> ...]

Exit codes:
    0 - every path matched (or none were NTFS-eligible, so nothing to compare)
    1 - not elevated, not Windows, or bad arguments
    2 - at least one path showed a real discrepancy

Known limitation: for a file with more than one hard link, the two
engines can legitimately disagree about *which* occurrence is flagged as
the "hardlink_dup" (and therefore which one shows the real size vs. zero)
-- each engine discovers the links in a different order, but the file's
*total* contribution to size/file_count still matches either way. This
script does not attempt to reconcile that per-occurrence ordering, so a
hard-link-heavy target (e.g. C:\\Windows\\WinSxS) may show a handful of
"hardlink_dup MISMATCH"/"size MISMATCH" pairs that are not real bugs --
sanity-check those specific paths by hand rather than treating every
reported line as equally actionable.
"""

import argparse
import queue
import sys
import threading
import time

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


def _flatten(node, out=None):
    """path -> node, for every node in the tree."""
    if out is None:
        out = {}
    out[node.path] = node
    for child in node.children:
        _flatten(child, out)
    return out


def _compare(compatible_root, turbo_root):
    """Return a list of human-readable discrepancy strings, empty if the
    two trees agree on every compared field for every node."""
    compat_by_path = _flatten(compatible_root)
    turbo_by_path = _flatten(turbo_root)

    discrepancies = []

    for path in sorted(set(compat_by_path) - set(turbo_by_path)):
        discrepancies.append(f"MISSING FROM TURBO: {path}")
    for path in sorted(set(turbo_by_path) - set(compat_by_path)):
        discrepancies.append(f"EXTRA IN TURBO: {path}")

    for path in sorted(set(compat_by_path) & set(turbo_by_path)):
        c, t = compat_by_path[path], turbo_by_path[path]
        for field in _COMPARED_FIELDS:
            c_val, t_val = getattr(c, field), getattr(t, field)
            if c_val != t_val:
                discrepancies.append(
                    f"{field} MISMATCH at {path}: compatible={c_val!r} turbo={t_val!r}"
                )

    return discrepancies


def compare_one(path):
    print(f"\n=== {path} ===")

    progress_q, cancel_event = queue.Queue(), threading.Event()
    print("Running Compatible engine...")
    start = time.perf_counter()
    compatible_root = compatible_scan(path, progress_q, cancel_event)
    compatible_elapsed = time.perf_counter() - start
    print(
        f"  {compatible_root.size:,} bytes, {compatible_root.file_count:,} files "
        f"in {compatible_elapsed:.1f}s"
    )

    progress_q, cancel_event = queue.Queue(), threading.Event()
    print("Running Turbo Scan...")
    turbo_root, report = scan_with_best_engine(
        path,
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )
    if report.engine != ENGINE_TURBO:
        print(
            f"SKIP: Turbo Scan did not actually run for this path "
            f"(fallback_reason={report.fallback_reason!r}) -- nothing to compare."
        )
        return None
    print(
        f"  {turbo_root.size:,} bytes, {turbo_root.file_count:,} files "
        f"in {report.elapsed_seconds:.1f}s"
    )
    if report.elapsed_seconds > 0:
        print(f"  speedup: {compatible_elapsed / report.elapsed_seconds:.1f}x")

    discrepancies = _compare(compatible_root, turbo_root)
    if discrepancies:
        print(f"MISMATCH ({len(discrepancies)} discrepancies):")
        for line in discrepancies[:50]:
            print(f"  - {line}")
        if len(discrepancies) > 50:
            print(f"  ... and {len(discrepancies) - 50} more")
        return False

    print("MATCH: Turbo Scan agrees with the Compatible engine.")
    return True


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
