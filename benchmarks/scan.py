#!/usr/bin/env python3
"""Benchmark the scanner against a generated folder tree it can be checked against.

    python benchmarks/scan.py --profile medium --output bench-v1.5.0.json
    python benchmarks/scan.py --profile medium --baseline bench-v1.5.0.json

Builds the synthetic folder tree for a profile and seed (see
generated_tree.py: the same seed and profile always produce the same files,
names, and sizes), scans it with the Compatible engine
(storage_scanner.scanner.scan), and then:

- checks the scan against what was generated: total size, file and folder
  counts, hard-link dedup, per-top-level-folder totals (which catch rollup
  bugs), and that symlinks/junctions were recorded as leaves rather than
  followed, and
- times several scans of the same tree and measures peak Python memory in
  a separate run under tracemalloc, which slows a scan too much to time
  alongside.

benchmarks/scale.py is the counterpart for synthetic volumes: it gates how
memory and database sizes grow with file count, without touching the disk.

Timings are warm-cache: the tree was just written, and an untimed
verification scan runs before the timed ones. They measure how fast the
scanner processes metadata, not how fast a disk seeks. Compare results only
from the same machine: a baseline from another machine, Python or worker
count is reported but not refused.

Turbo Scan isn't benchmarked here. It reads the whole NTFS volume, not the
generated folder, and needs an elevated prompt; compare_scan_engines.py is
its correctness check.

Exit codes:
    0 - the scan matched the generated tree (and, with --baseline, wasn't
        slower than --max-slowdown allows)
    1 - bad arguments, or a baseline from a different profile/seed/tree
        (refused before anything is generated whenever the file alone shows it)
    2 - the scan didn't match the generated tree
    3 - slower than the baseline by more than --max-slowdown

Standard library plus this repo's own storage_scanner package.
"""

import argparse
import json
import os
import platform
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import asdict
from pathlib import Path

from generated_tree import PROFILES, generate_tree, remove_tree, verify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Bump whenever generated_tree.py's generator or the result format changes: a
# result from a different schema describes a different tree and can't serve as
# a baseline.
SCHEMA_VERSION = 1


def _scan_once(path, workers):
    from storage_scanner.scanner import scan

    start = time.perf_counter()
    node = scan(path, queue.SimpleQueue(), threading.Event(), workers=workers)
    return node, time.perf_counter() - start


def _peak_traced_bytes(path, workers):
    tracemalloc.start()
    try:
        _scan_once(path, workers)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _git_revision():
    here = Path(__file__).resolve().parent
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=here, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit + ("+dirty" if dirty else "")


def run_benchmark(profile, seed, runs, workers, parent_dir, keep=False, log=print):
    from storage_scanner.version import __version__

    spec = PROFILES[profile]
    root = tempfile.mkdtemp(prefix="storage-scanner-bench-", dir=parent_dir)
    os.rmdir(root)  # generate_tree creates it, so a stale one is never reused
    log(f"Generating the '{profile}' tree (seed {seed}) in {root} ...")
    start = time.perf_counter()
    manifest = generate_tree(root, spec, seed)
    log(
        f"  {manifest.total_files:,} files, {manifest.dir_count:,} folders, "
        f"{manifest.total_size:,} bytes in {time.perf_counter() - start:.1f}s; "
        f"links created: {', '.join(manifest.link_kinds) or 'none'}"
    )
    try:
        log("Verifying a scan against the generated tree ...")
        node, _elapsed = _scan_once(root, workers)
        problems = verify(node, manifest)
        del node
        timings = []
        for i in range(runs):
            _node, elapsed = _scan_once(root, workers)
            timings.append(elapsed)
            log(f"  run {i + 1}/{runs}: {elapsed:.3f}s")
        log("Measuring peak memory (tracemalloc) ...")
        peak = _peak_traced_bytes(root, workers)
    finally:
        if keep:
            log(f"Kept the tree at {root}")
        else:
            remove_tree(manifest)

    median = statistics.median(timings)
    return {
        "schema": SCHEMA_VERSION,
        "app_version": __version__,
        "git_revision": _git_revision(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "workers": workers,
        "profile": profile,
        "seed": seed,
        "spec": asdict(spec),
        "tree": {
            "files": manifest.total_files,
            "folders": manifest.dir_count,
            "bytes": manifest.total_size,
            "hardlink_extras": manifest.hardlink_extras,
            "links": manifest.link_kinds,
        },
        "correct": not problems,
        "problems": problems,
        "runs_seconds": [round(t, 4) for t in timings],
        "median_seconds": round(median, 4),
        "files_per_second": round(manifest.total_files / median) if median else None,
        "peak_traced_bytes": peak,
    }


def check_baseline(baseline, profile, seed):
    """Refuse a baseline this run could never be compared against, before
    spending minutes generating and scanning a tree. Raises ValueError.

    Only what the file alone can show: whether the generated tree matches
    (which links this machine could create) is known after the run, and
    compare_to_baseline() checks it then."""
    if not isinstance(baseline, dict):
        raise ValueError("not a benchmark result")
    median = baseline.get("median_seconds")
    if not isinstance(median, (int, float)) or median <= 0:
        raise ValueError(f"median_seconds is {median!r}, not a positive number")
    for key, value in (("schema", SCHEMA_VERSION), ("profile", profile), ("seed", seed)):
        if baseline.get(key) != value:
            raise ValueError(
                f"it has {key} {baseline.get(key)!r}, this run {value!r}; "
                "rerun it with the same --profile/--seed"
            )


def compare_to_baseline(result, baseline, max_slowdown):
    """(regressed, message, warnings). Raises ValueError when the two
    results describe different trees and so can't be compared at all."""
    for key in ("schema", "profile", "seed", "tree"):
        if result.get(key) != baseline.get(key):
            raise ValueError(
                f"baseline has a different {key} ({baseline.get(key)!r} vs {result.get(key)!r}); "
                "rerun it with the same --profile/--seed"
            )
    warnings = [
        f"{key} differs from the baseline ({baseline.get(key)!r} vs {result.get(key)!r}); "
        "timings may not be comparable"
        for key in ("platform", "python", "cpu_count", "workers")
        if result.get(key) != baseline.get(key)
    ]
    ratio = result["median_seconds"] / baseline["median_seconds"]
    regressed = ratio > 1 + max_slowdown
    message = (
        f"median {result['median_seconds']:.3f}s vs baseline {baseline['median_seconds']:.3f}s "
        f"({(ratio - 1) * 100:+.1f}%, limit +{max_slowdown * 100:.0f}%)"
    )
    return regressed, message, warnings


def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", choices=sorted(PROFILES), default="small")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--runs", type=_positive_int, default=3, help="timed scans (default 3)")
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        help="scan threads (default: the app's own choice)",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="parent folder for the generated tree (default: the system temp folder); "
        "put it on the drive you want to measure",
    )
    parser.add_argument("--keep", action="store_true", help="leave the generated tree in place")
    parser.add_argument("--output", help="write the result as JSON to this file")
    parser.add_argument("--baseline", help="a previous --output file to compare against")
    parser.add_argument(
        "--max-slowdown",
        type=float,
        default=0.25,
        help="fail when the median is this fraction slower than the baseline's (default 0.25)",
    )
    args = parser.parse_args(argv)

    # Checked up front: a large profile takes minutes, too long to lose to a typo.
    if args.output and not Path(args.output).resolve().parent.is_dir():
        print(f"Output folder for {args.output} doesn't exist", file=sys.stderr)
        return 1
    if args.dir and not os.path.isdir(args.dir):
        print(f"--dir {args.dir} isn't an existing folder", file=sys.stderr)
        return 1

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            check_baseline(baseline, args.profile, args.seed)
        except (OSError, ValueError) as exc:
            print(f"Can't use baseline {args.baseline}: {exc}", file=sys.stderr)
            return 1

    result = run_benchmark(
        args.profile, args.seed, args.runs, args.workers, args.dir, keep=args.keep
    )
    print(
        f"Median {result['median_seconds']:.3f}s ({result['files_per_second']:,} files/s), "
        f"peak traced memory {result['peak_traced_bytes'] / 1024 / 1024:.1f} MiB"
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Wrote {args.output}")

    if not result["correct"]:
        print("SCAN DOES NOT MATCH THE GENERATED TREE:", file=sys.stderr)
        for problem in result["problems"]:
            print(f"  {problem}", file=sys.stderr)
        return 2
    print("Scan matched the generated tree exactly.")

    if baseline is not None:
        try:
            regressed, message, warnings = compare_to_baseline(result, baseline, args.max_slowdown)
        except ValueError as exc:
            print(f"Not comparable: {exc}", file=sys.stderr)
            return 1
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        if regressed:
            print(f"SLOWER THAN BASELINE: {message}", file=sys.stderr)
            return 3
        print(f"Within the baseline: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
