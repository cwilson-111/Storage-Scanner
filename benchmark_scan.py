#!/usr/bin/env python3
"""Benchmark the scanner against a generated folder tree it can be checked against.

    python benchmark_scan.py --profile medium --output bench-v1.5.0.json
    python benchmark_scan.py --profile medium --baseline bench-v1.5.0.json

Builds a synthetic folder tree from a seed (the same seed and profile always
produce the same files, names, and sizes), scans it with the Compatible
engine (storage_scanner.scanner.scan), and then:

- checks the scan against what was generated: total size, file and folder
  counts, hard-link dedup, per-top-level-folder totals (which catch rollup
  bugs), and that symlinks/junctions were recorded as leaves rather than
  followed, and
- times several scans of the same tree and measures peak Python memory in
  a separate run under tracemalloc, which slows a scan too much to time
  alongside.

The generated tree covers the edge cases the scanner makes promises about:
a random nested tree, empty folders, a deep folder chain, hard links (each
counted once), a symlink or junction to a folder with files in it (never
followed), and unusual names (Unicode, spaces, leading dots, a 100-character
name). A link the OS won't let this account create is skipped, and the
result records which ones were created. File contents are zeros; only the
metadata matters to a scan.

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
    2 - the scan didn't match the generated tree
    3 - slower than the baseline by more than --max-slowdown

Standard library plus this repo's own storage_scanner package.
"""

import argparse
import json
import os
import platform
import queue
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from pathlib import Path

from storage_scanner.scanner import scan
from storage_scanner.version import __version__

# Bump whenever the generator or the result format changes: a result from a
# different schema describes a different tree and can't serve as a baseline.
SCHEMA_VERSION = 1

_IS_WINDOWS = sys.platform == "win32"

_EXTENSIONS = (".txt", ".log", ".jpg", ".png", ".pdf", ".docx", ".zip", ".dll", ".json", "")
_ODD_NAMES = (
    "ünïcødé ファイル.txt",
    "name with  spaces.txt",
    ".hidden-file",
    "no_extension",
    "many.dots.in.the.name.tar.gz",
    # 100 characters: long for a single name, yet its full path still fits in
    # Windows' 260-character MAX_PATH (long-path support is off by default)
    # from any reasonably placed folder, pytest's deep tmp_path included.
    "x" * 96 + ".bin",
)


@dataclass(frozen=True)
class TreeSpec:
    dirs: int  # folders in the random nested tree
    files: int  # files spread across those folders
    hardlinks: int  # files that each get one extra hard link
    chain_depth: int  # folders in the single deep chain


PROFILES = {
    "small": TreeSpec(dirs=200, files=2_000, hardlinks=20, chain_depth=40),
    "medium": TreeSpec(dirs=2_000, files=20_000, hardlinks=100, chain_depth=60),
    "large": TreeSpec(dirs=10_000, files=100_000, hardlinks=200, chain_depth=60),
}


@dataclass
class Manifest:
    """What a correct scan of the generated tree must report.

    `top_level` maps each folder directly under the root to its expected
    [size, file_count]. Hard links and their originals sit under the same
    top-level folder, so those totals don't depend on which occurrence a
    scan happens to count first.
    """

    root: str
    top_level: "dict[str, list[int]]" = field(default_factory=dict)
    dir_count: int = 0  # folders below the root
    hardlink_extras: int = 0  # extra hard-link entries actually created
    links: "list[str]" = field(default_factory=list)  # symlinks/junctions created
    link_kinds: "list[str]" = field(default_factory=list)

    @property
    def total_size(self):
        return sum(size for size, _files in self.top_level.values())

    @property
    def total_files(self):
        return sum(files for _size, files in self.top_level.values())


def _file_size(rng):
    """Mostly small files with a long tail, so a large profile stays around
    a gigabyte on disk instead of tens of them."""
    roll = rng.random()
    if roll < 0.90:
        return rng.randint(0, 4096)
    if roll < 0.999:
        return rng.randint(4097, 64 * 1024)
    return rng.randint(64 * 1024 + 1, 8 * 1024 * 1024)


def _write_file(path, size):
    with open(path, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")


def _mkdir(path, manifest):
    os.mkdir(path)
    manifest.dir_count += 1


def _add_files(manifest, top, entries):
    """Write (path, size) entries and count them under top-level folder `top`."""
    totals = manifest.top_level.setdefault(top, [0, 0])
    for path, size in entries:
        _write_file(path, size)
        totals[0] += size
        totals[1] += 1


def _gen_random_tree(base, spec, rng, manifest):
    _mkdir(base, manifest)
    dirs = [base]
    for i in range(spec.dirs):
        parent = rng.choice(dirs)
        path = os.path.join(parent, f"dir{i:05d}")
        _mkdir(path, manifest)
        dirs.append(path)
    # Folders the loop below never picks stay empty, which is realistic too.
    _add_files(
        manifest,
        "tree",
        (
            (os.path.join(rng.choice(dirs), f"f{i:06d}{rng.choice(_EXTENSIONS)}"), _file_size(rng))
            for i in range(spec.files)
        ),
    )


def _gen_chain(base, spec, rng, manifest):
    # One-letter names keep the chain under Windows' 260-character MAX_PATH
    # from a normal temp folder; the depth, not the length, is the point.
    path = base
    _mkdir(path, manifest)
    for _ in range(spec.chain_depth):
        path = os.path.join(path, "d")
        _mkdir(path, manifest)
    _add_files(manifest, "chain", [(os.path.join(path, "bottom.bin"), _file_size(rng))])


def _gen_hardlinks(base, spec, rng, manifest):
    originals_dir = os.path.join(base, "originals")
    links_dir = os.path.join(base, "links")
    for path in (base, originals_dir, links_dir):
        _mkdir(path, manifest)
    originals = [
        (os.path.join(originals_dir, f"h{i:04d}.bin"), _file_size(rng))
        for i in range(spec.hardlinks)
    ]
    _add_files(manifest, "hardlinks", originals)
    totals = manifest.top_level["hardlinks"]
    for i, (original, _size) in enumerate(originals):
        try:
            os.link(original, os.path.join(links_dir, f"h{i:04d}.bin"))
        except OSError:  # e.g. FAT/exFAT, or a filesystem without hard links
            break
        totals[1] += 1  # an extra entry, but its bytes are only counted once
        manifest.hardlink_extras += 1


def _gen_names(base, rng, manifest):
    _mkdir(base, manifest)
    _add_files(
        manifest, "names", [(os.path.join(base, name), _file_size(rng)) for name in _ODD_NAMES]
    )
    _mkdir(os.path.join(base, "empty"), manifest)


def _try_link(manifest, kind, create, path):
    """Create one symlink/junction; a scan must record it as a single leaf
    entry with its own lstat() size, never follow it."""
    try:
        create()
    except (OSError, NotImplementedError):
        return  # no symlink privilege (Windows without Developer Mode), etc.
    totals = manifest.top_level["links"]
    totals[0] += os.lstat(path).st_size
    totals[1] += 1
    manifest.links.append(path)
    manifest.link_kinds.append(kind)


def _gen_links(base, manifest):
    target = os.path.join(base, "target")
    for path in (base, target):
        _mkdir(path, manifest)
    # Enough bytes behind the links that following one would be obvious.
    _add_files(
        manifest, "links", [(os.path.join(target, f"t{i}.bin"), 1024 * (i + 1)) for i in range(5)]
    )

    # Relative targets, so a POSIX symlink's lstat size (its target's length)
    # doesn't depend on where the tree was generated.
    dir_link = os.path.join(base, "dir-symlink")
    _try_link(
        manifest,
        "dir-symlink",
        lambda: os.symlink("target", dir_link, target_is_directory=True),
        dir_link,
    )
    file_link = os.path.join(base, "file-symlink")
    _try_link(
        manifest,
        "file-symlink",
        lambda: os.symlink(os.path.join("target", "t0.bin"), file_link),
        file_link,
    )
    if _IS_WINDOWS:
        import _winapi  # CPython's own junction helper; needs no privilege

        junction = os.path.join(base, "junction")
        _try_link(manifest, "junction", lambda: _winapi.CreateJunction(target, junction), junction)


def generate_tree(root, spec, seed):
    """Create the tree for (spec, seed) under the new folder `root` and
    return its Manifest. `root` must not already exist."""
    rng = random.Random(seed)
    os.mkdir(root)
    manifest = Manifest(root=root)
    try:
        _gen_random_tree(os.path.join(root, "tree"), spec, rng, manifest)
        _gen_chain(os.path.join(root, "chain"), spec, rng, manifest)
        _gen_hardlinks(os.path.join(root, "hardlinks"), spec, rng, manifest)
        _gen_names(os.path.join(root, "names"), rng, manifest)
        _gen_links(os.path.join(root, "links"), manifest)
    except BaseException:
        remove_tree(manifest)  # don't leave a half-built tree behind
        raise
    return manifest


def remove_tree(manifest):
    """Delete a generated tree, removing its links first so nothing ever
    deletes through one into its target."""
    for path, kind in zip(manifest.links, manifest.link_kinds):
        if _IS_WINDOWS and kind in ("dir-symlink", "junction"):
            os.rmdir(path)  # removes the link itself, not the folder it points to
        else:
            os.unlink(path)
    shutil.rmtree(manifest.root)


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def verify(root_node, manifest):
    """Every way the scan differs from the generated tree, as readable
    strings; an empty list means it matched exactly."""
    problems = []

    def check(label, scanned, expected):
        if scanned != expected:
            problems.append(f"{label}: scanned {scanned}, expected {expected}")

    check("total size", root_node.size, manifest.total_size)
    check("file count", root_node.file_count, manifest.total_files)
    nodes = list(_walk(root_node))
    check("folder count", sum(n.is_dir for n in nodes) - 1, manifest.dir_count)
    check("hard-link duplicates", sum(n.hardlink_dup for n in nodes), manifest.hardlink_extras)
    check("unreadable entries", sum(n.error for n in nodes), 0)

    children = {child.name: child for child in root_node.children}
    for name, (size, files) in sorted(manifest.top_level.items()):
        node = children.get(name)
        if node is None:
            problems.append(f"{name}: missing from the scan")
            continue
        check(f"{name}/ size", node.size, size)
        check(f"{name}/ file count", node.file_count, files)
    problems.extend(
        f"{name}: in the scan but never generated"
        for name in sorted(set(children) - set(manifest.top_level))
    )

    by_path = {os.path.normcase(n.path): n for n in nodes}
    for path in manifest.links:
        node = by_path.get(os.path.normcase(path))
        if node is None:
            problems.append(f"{path}: link missing from the scan")
        elif node.is_dir or node.children:
            problems.append(f"{path}: link was followed instead of recorded as a leaf")
    return problems


def _scan_once(path, workers):
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

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Can't read baseline {args.baseline}: {exc}", file=sys.stderr)
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
