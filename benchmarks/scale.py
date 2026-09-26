#!/usr/bin/env python3
"""Scale benchmarks: how Storage Scanner's memory, databases and rescans
grow with the number of files, so a change that makes any of them worse
at scale is caught before release.

Every scenario builds the same synthetic volume layout (scale_volume.py: a
breadth-first tree of folders holding FILES_PER_DIR files each) and runs in
its own subprocess, so each reports its own peak memory. The scan-history
scenarios are in scale_history.py.

Scenarios:
  tree_memory       the scanned tree held in memory (storage_scanner.models.Node)
  history           scan history after SCHEDULED_SCANS repeat scans of one folder
  history_retention scan history after DAILY_SCANS daily scheduled scans (over
                    two years, simulated clock): what retention keeps
  history_20k       saving a 20,001-folder scan to history twice and comparing
                    the two, whatever --files is (the 1M-file volume's folders)
  turbo_rescan      the Turbo Scan cache: bytes per record, and what a rescan of
                    the whole volume vs. one small folder has to load and build
  compatible_scan   a real directory tree on disk scanned by the Compatible
                    engine (only with --disk-files; creating files is slow)

Size and count metrics (bytes per file/record/scan, records loaded, scans
kept, SQLite VM steps per folder row) are reproducible across runs and
machines, so `--check` gates them against baseline.json. Timings and peak
memory are reported but never gated: they're too noisy on shared CI runners
to fail a build on. VM steps stand in for time where a slow query is the
regression to catch: comparing two history scans by folder path text took
over a minute at 20k folders, and its step count showed it just as well.

Usage:
  python benchmarks/scale.py                        # all scenarios, 100k files
  python benchmarks/scale.py --files 1000000        # a bigger volume
  python benchmarks/scale.py --disk-files 50000     # also scan real files
  python benchmarks/scale.py --check                # CI gate vs. baseline.json
  python benchmarks/scale.py --write-baseline       # after an intended change

Exit codes: 0 ok, 1 a gated metric regressed past the tolerance, 2 a
scenario failed to run.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc

from scale_history import (
    scenario_history,
    scenario_history_20k,
    scenario_history_retention,
)
from scale_volume import (
    VOLUME_ROOT,
    build_node_tree,
    database_bytes,
    layout,
    peak_rss_bytes,
    small_subtree_parts,
    synthetic_records,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")
# Gated metrics are all "lower is better". The tolerance absorbs small
# differences between interpreter versions (CI runs 3.12).
TOLERANCE = 0.15
GATED = (
    "tree_bytes_per_file",
    "history_bytes_per_scan",
    "history_daily_scans_kept",
    "history_daily_scans_db_bytes",
    "history_20k_save_steps_per_row",
    "history_20k_growth_steps_per_row",
    "turbo_cache_bytes_per_record",
    "turbo_small_subtree_records_loaded",
)
IN_MEMORY_SCENARIOS = ("tree_memory", "history", "history_retention", "history_20k", "turbo_rescan")


# -- Scenarios (each runs in its own subprocess) ------------------------------ #


def scenario_tree_memory(n_files, _workdir):
    # Imported before tracing starts: module loading (and compiling, when
    # there's no __pycache__ yet) would otherwise count as tree memory.
    import storage_scanner.models  # noqa: F401
    import storage_scanner.scanner  # noqa: F401

    tracemalloc.start()
    start = time.perf_counter()
    root = build_node_tree(n_files)
    elapsed = time.perf_counter() - start
    held, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert root.file_count == n_files
    return {
        "tree_bytes_per_file": round(held / n_files, 1),
        "tree_build_seconds": round(elapsed, 3),
    }


def scenario_turbo_rescan(n_files, workdir):
    from storage_scanner import mft_scan, turbo_cache

    turbo_cache.DB_NAME = os.path.join(workdir, "turbo_scan_cache.db")
    turbo_cache.init_cache_db()
    records = synthetic_records(n_files)
    serial = 0x5CA1E
    root_frn = records[0].frn

    start = time.perf_counter()
    turbo_cache.save_full_scan(serial, VOLUME_ROOT, root_frn, 1024, records)
    save_seconds = time.perf_counter() - start
    cache_bytes = database_bytes(str(turbo_cache.DB_NAME))
    record_count = len(records)
    del records

    def rescan(parts):
        """What a Turbo rescan does once the cache is current (see
        turbo_read._subtree_from_cache): resolve the folder, load its
        records, build and finalize its tree."""
        start = time.perf_counter()
        target, actual = turbo_cache.find_record_by_path(serial, root_frn, list(parts))
        loaded = turbo_cache.load_subtree_records(serial, target)
        tree, _orphans, row_frns = mft_scan.build_tree(
            loaded,
            root_path=os.path.join(VOLUME_ROOT, *actual),
            root_record_number=target.frn & 0x0000FFFFFFFFFFFF,
        )
        mft_scan.finalize_subtree(tree, row_frns)
        return time.perf_counter() - start, len(loaded), tree

    whole_seconds, _whole_loaded, whole = rescan(())
    assert whole.file_count == n_files
    small_seconds, small_loaded, small = rescan(small_subtree_parts(n_files))

    return {
        "turbo_cache_bytes_per_record": round(cache_bytes / record_count, 1),
        "turbo_small_subtree_records_loaded": small_loaded,
        "turbo_small_subtree_files": small.file_count,
        "turbo_save_seconds": round(save_seconds, 3),
        "turbo_whole_rescan_seconds": round(whole_seconds, 3),
        "turbo_small_rescan_seconds": round(small_seconds, 3),
    }


def _disk_tree(n_files):
    """A real directory tree of n_files empty files, created once and reused
    (creating files is far slower than scanning them)."""
    base = os.path.join(tempfile.gettempdir(), "storage-scanner-bench", f"tree-{n_files}")
    marker = os.path.join(base, ".complete")
    if os.path.exists(marker):
        return base
    for parts, count in layout(n_files):
        folder = os.path.join(base, *parts)
        os.makedirs(folder, exist_ok=True)
        for index in range(count):
            with open(os.path.join(folder, f"file_{index:04d}.dat"), "wb"):
                pass
    with open(marker, "w", encoding="utf-8"):
        pass
    return base


def scenario_compatible_scan(n_files, _workdir):
    from storage_scanner import scanner

    base = _disk_tree(n_files)
    start = time.perf_counter()
    root = scanner.scan(base, queue.Queue(), threading.Event())
    elapsed = time.perf_counter() - start
    return {
        "compatible_scan_seconds": round(elapsed, 3),
        "compatible_files_per_second": round(root.file_count / elapsed) if elapsed else None,
    }


SCENARIOS = {
    "tree_memory": scenario_tree_memory,
    "history": scenario_history,
    "history_retention": scenario_history_retention,
    "history_20k": scenario_history_20k,
    "turbo_rescan": scenario_turbo_rescan,
    "compatible_scan": scenario_compatible_scan,
}


def _run_one(name, n_files):
    """Child-process entry: run one scenario, print its metrics as JSON."""
    with tempfile.TemporaryDirectory(prefix="storage-scanner-bench-") as workdir:
        metrics = SCENARIOS[name](n_files, workdir)
    metrics[f"{name}_peak_rss_bytes"] = peak_rss_bytes()
    print(json.dumps(metrics))


def run_scenario(name, n_files):
    """Run one scenario in a fresh interpreter; returns its metrics."""
    result = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--one", name, "--files", str(n_files)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# -- Reporting and the gate --------------------------------------------------- #


def compare(metrics, baseline, tolerance=TOLERANCE):
    """(regressions, improvements): gated metrics that got worse past the
    tolerance, and ones that got better past it (time to update the
    baseline). Each item is (name, baseline value, current value)."""
    regressions, improvements = [], []
    for name in GATED:
        if name not in metrics or name not in baseline:
            continue
        old, new = baseline[name], metrics[name]
        if new > old * (1 + tolerance):
            regressions.append((name, old, new))
        elif new < old * (1 - tolerance):
            improvements.append((name, old, new))
    return regressions, improvements


def _print_metrics(metrics, baseline=None):
    width = max(len(name) for name in metrics)
    for name, value in metrics.items():
        line = f"  {name:<{width}}  {value:>14,}" if isinstance(value, (int, float)) else ""
        if baseline and name in baseline:
            line += f"   (baseline {baseline[name]:,})"
        if name in GATED:
            line += "   [gated]"
        print(line or f"  {name:<{width}}  {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--files", type=int, default=None, help="files in the synthetic volume")
    parser.add_argument(
        "--disk-files",
        type=int,
        default=0,
        help="also scan a real tree of this many files with the Compatible engine",
    )
    parser.add_argument("--check", action="store_true", help="gate against baseline.json")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--one", choices=sorted(SCENARIOS), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.one:
        _run_one(args.one, args.files)
        return 0

    baseline_doc = None
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, encoding="utf-8") as f:
            baseline_doc = json.load(f)

    if args.check or args.write_baseline:
        # The gate compares like with like: the baseline's own volume size.
        n_files = args.files or (baseline_doc or {}).get("files") or 20_000
    else:
        n_files = args.files or 100_000

    print(f"Synthetic volume: {n_files:,} files, {len(layout(n_files)) - 1:,} folders")
    metrics = {}
    try:
        for name in IN_MEMORY_SCENARIOS:
            metrics.update(run_scenario(name, n_files))
        if args.disk_files:
            metrics.update(run_scenario("compatible_scan", args.disk_files))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    baseline = (baseline_doc or {}).get("metrics", {})
    same_size = baseline_doc is not None and baseline_doc.get("files") == n_files
    _print_metrics(metrics, baseline if same_size else None)

    if args.write_baseline:
        doc = {
            "files": n_files,
            "python": ".".join(map(str, sys.version_info[:2])),
            "metrics": {name: metrics[name] for name in GATED},
        }
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print(f"Wrote {os.path.relpath(BASELINE_PATH, ROOT)}")
        return 0

    if args.check:
        if not same_size:
            print("No baseline for this volume size; run with --write-baseline.", file=sys.stderr)
            return 2
        regressions, improvements = compare(metrics, baseline)
        for name, old, new in improvements:
            print(f"Improved: {name} {old:,} -> {new:,}; consider --write-baseline.")
        for name, old, new in regressions:
            print(
                f"REGRESSION: {name} {old:,} -> {new:,} (over {TOLERANCE:.0%} worse)",
                file=sys.stderr,
            )
        return 1 if regressions else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
