#!/usr/bin/env python3
"""The P1-3 checklist: verifies Turbo Scan and its cache on this machine's
real NTFS drive, end to end, and prints a pass/fail report that it also
saves to a file, ready for NEURAL_STORAGE_MATRIX_PROJECT_ROADMAP.md.

Must be run from an elevated ("Run as Administrator") terminal, like
compare_scan_engines.py and for the same reason; it refuses otherwise,
before creating anything. It writes only inside a scratch folder it makes
in %TEMP% and deletes at the end: its fixtures (turbo_checklist_fixtures),
and a Turbo Scan cache of its own -- so its first scan reads the whole MFT,
and the app's cache is left as it was. Everything else it only reads.

The steps, in order (each scan of the tracked folder builds on the cache
the one before it left):
1. A full scan through the elevated helper the unelevated app starts
   (Storage-Scanner.py --mft-scan), its progress file read the way the app
   reads it, with a file grown during the MFT read, after its record.
2. Files created, grown, renamed, moved and deleted, then an incremental
   scan in this process (the elevated app's path), with a file grown while
   it runs. It must show every change, and the growth during step 1.
3. An incremental scan through the helper: it must show the growth during
   step 2 and match the Compatible engine field for field.
4.-8. compare_scan_engines.compare_path() against the Compatible engine on
   a compressed folder (compact /c and compact /exe), a junction (mklink /J)
   asked for directly, the folder holding it and a file symbolic link
   (mklink), %SystemRoot% and the folder holding the user profiles.

Usage:
    python turbo_checklist.py [--results FILE]

Exit codes: 0 every step passed; 1 refused (not Windows, not elevated, or
%TEMP% not on a fixed NTFS drive); 2 a step failed.
"""

import argparse
import json
import logging
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from compare_scan_engines import UNEXPLAINED, _compare, _flatten, compare_path
from storage_scanner import turbo_cache
from storage_scanner.drive_info import get_volume_root, is_ntfs_fixed_drive
from storage_scanner.elevation import _WAIT_POLL_MS, _relay_progress_file
from storage_scanner.history_db import APP_NAME
from storage_scanner.logging_setup import LOG_FILE_NAME, logger
from storage_scanner.models import iter_folders
from storage_scanner.platform_support import IS_ROOT, IS_WINDOWS
from storage_scanner.scanner import scan as compatible_scan
from storage_scanner.serialization import dict_to_node
from storage_scanner.turbo_read import PHASE_LOADING_CACHE, PHASE_READING_MFT, MftRead
from storage_scanner.turbo_scan import ENGINE_TURBO, scan_with_best_engine
from turbo_checklist_fixtures import (
    BESIDE_THE_JUNCTION,
    COMPRESSED_FILES,
    GROWN_DURING_FULL_READ,
    GROWN_DURING_INCREMENTAL,
    JUNCTION_TARGET_FILES,
    KNOWN_CHANGES,
    TRACKED_FILES,
    ScanWatcher,
    describe_change,
    expected_sizes,
    file_sizes,
    make_change,
    make_file_link,
    make_junction,
    remove_scratch,
    run_tool,
    size_problems,
    write_files,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENTRY_SCRIPT = os.path.join(_HERE, "Storage-Scanner.py")
_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF  # an NTFS file ID's MFT record number
_LOG_PROBLEM_LEVELS = ("WARNING", "ERROR", "CRITICAL")


@dataclass
class StepResult:
    title: str
    passed: bool
    details: list = field(default_factory=list)


def format_report(started, environment, results, notes=()):
    """The report: a summary line, then one bullet per step with its
    details below it, in the roadmap's style."""
    passed = sum(result.passed for result in results)
    lines = [
        f"Turbo Scan checklist (P1-3), {started:%Y-%m-%d %H:%M}: "
        f"{passed} of {len(results)} steps passed.",
        environment,
        "",
    ]
    for number, result in enumerate(results, 1):
        lines.append(f"- {'PASS' if result.passed else 'FAIL'} {number}. {result.title}")
        lines += [f"  - {detail}" for detail in result.details]
    lines += [f"Note: {note}" for note in notes]
    return "\n".join(lines) + "\n"


def refusal_reason():
    """Why the checklist can't run here, or None."""
    if not IS_WINDOWS:
        return "This checklist is Windows-only: Turbo Scan reads NTFS."
    if not IS_ROOT:
        command = subprocess.list2cmdline([sys.executable, os.path.abspath(__file__)])
        return (
            'Must be run from an elevated ("Run as Administrator") terminal: Turbo '
            "Scan reads the raw volume, and this checklist never asks for elevation "
            f"itself. From one, run:\n    {command}"
        )
    if not is_ntfs_fixed_drive(tempfile.gettempdir()):
        return f"The temp folder {tempfile.gettempdir()} isn't on a fixed NTFS drive."
    return None


class _WarningsSeen(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class Checklist:
    """The steps, run in order against one scratch folder."""

    def __init__(self, scratch):
        self.scratch = scratch
        self.tracked = os.path.join(scratch, "tracked")
        self.app_data = os.path.join(scratch, "appdata")
        self.logs = os.path.join(scratch, "logs")
        self.linked = os.path.join(scratch, "linked")
        self.junction = os.path.join(self.linked, "junction")
        self.file_link = os.path.join(self.linked, "file_link.bin")
        self.expected = expected_sizes()
        self.helper_log_lines = 0

    def steps(self):
        # Spelled as on disk, as Turbo Scan's tree spells them (%SystemRoot%
        # is often C:\WINDOWS), so the two engines' paths compare.
        windows = os.path.realpath(os.environ.get("SYSTEMROOT", "C:\\Windows"))
        users = os.path.realpath(os.path.dirname(os.path.expanduser("~")))
        return [
            ("Full scan through the elevated helper, a file grown mid-read", self.full_scan),
            ("Incremental scan in this process after known changes", self.incremental_scan),
            ("Incremental scan through the elevated helper", self.helper_incremental_scan),
            ("Compressed folder (compact /c, compact /exe)", self.compressed_folder),
            ("Junction asked for directly", self.junction_root),
            ("Folder holding a junction and a file symlink", self.junction_parent),
            (windows, lambda: self._engines(windows, time.time())[:2]),
            (users, lambda: self._engines(users, time.time())[:2]),
        ]

    def run(self):
        write_files(self.tracked, TRACKED_FILES)
        cache_folder = Path(self.app_data) / APP_NAME
        cache_folder.mkdir(parents=True)
        # The cache the helper finds through LOCALAPPDATA (_helper_scan).
        real_cache = turbo_cache.DB_NAME
        turbo_cache.DB_NAME = cache_folder / "turbo_scan_cache.db"
        warnings = _WarningsSeen()
        logger.addHandler(warnings)
        results = []
        try:
            steps = self.steps()
            for number, (title, step) in enumerate(steps, 1):
                print(f"[{number}/{len(steps)}] {title}", flush=True)
                seen = len(warnings.messages)
                failure = None
                try:
                    passed, details = step()
                except Exception as exc:  # noqa: BLE001 - one step failing mustn't stop the rest
                    passed, details, failure = False, [f"error: {exc}"], exc
                details += [f"log: {message}" for message in warnings.messages[seen:][:3]]
                if failure is not None:
                    logger.warning("Checklist step %r failed", title, exc_info=failure)
                results.append(StepResult(title, passed, details))
                print("  " + "\n  - ".join(["PASS" if passed else "FAIL", *details]), flush=True)
        finally:
            logger.removeHandler(warnings)
            turbo_cache.DB_NAME = real_cache
        return results

    def _helper_scan(self, path, watcher):
        """(Node, MftRead, seconds) from Storage-Scanner.py --mft-scan, started
        with elevation.run_elevated_scan_windows's arguments but without its
        UAC prompt (this process is elevated already), its progress file
        relayed into `watcher` the way that function reads it. Raises with
        the helper's own error when it fails."""
        output = os.path.join(self.scratch, "helper_result.json")
        progress = os.path.join(self.scratch, "helper_progress.json")
        args = [sys.executable, _ENTRY_SCRIPT, "--mft-scan", get_volume_root(path)]
        args += ["--subtree", path, "--output", output, "--progress-file", progress]
        env = dict(os.environ, LOCALAPPDATA=self.app_data, STORAGE_SCANNER_LOG_DIR=self.logs)
        start = time.perf_counter()
        process = subprocess.Popen(
            args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        last_phase = None
        while True:
            try:
                exit_code = process.wait(timeout=_WAIT_POLL_MS / 1000)
                break
            except subprocess.TimeoutExpired:
                last_phase = _relay_progress_file(progress, watcher, last_phase)
        seconds = time.perf_counter() - start
        try:
            with open(output, encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, ValueError) as exc:
            result = {"error": f"no readable result ({exc})"}
        for leftover in (output, progress, progress + ".tmp"):
            if os.path.exists(leftover):
                os.remove(leftover)
        if exit_code != 0 or "error" in result:
            raise RuntimeError(f"the helper exited with code {exit_code}: {result.get('error')}")
        return dict_to_node(result["node"]), MftRead.from_dict(result["mft_read"]), seconds

    def _helper_warnings(self):
        """The helper's log lines at WARNING or above since the last call
        (a line is "<date> <time> <level> ...")."""
        try:
            with open(os.path.join(self.logs, LOG_FILE_NAME), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        new, self.helper_log_lines = lines[self.helper_log_lines :], len(lines)
        return [
            f"helper log: {line}"
            for line in new
            if len(line.split()) > 2 and line.split()[2] in _LOG_PROBLEM_LEVELS
        ]

    def _sizes(self, node, scan, passed, details):
        problems = size_problems(file_sizes(node), self.expected[scan])
        details += problems or ["every file at the size the changes leave it"]
        return passed and not problems, details + self._helper_warnings()

    def full_scan(self):
        grown = GROWN_DURING_FULL_READ
        record = os.stat(os.path.join(self.tracked, grown.name)).st_ino & _RECORD_NUMBER_MASK
        watcher = ScanWatcher(self.tracked, grown, PHASE_READING_MFT, after=record)
        try:
            node, mft_read, seconds = self._helper_scan(self.tracked, watcher)
        finally:
            missed = watcher.make_change_if_missed()
        details = [
            f"{mft_read.describe()} in {seconds:.1f}s; {len(watcher.phases):,} progress "
            "updates read from the helper's progress file"
        ]
        if missed:
            details.append(f"{grown.name} not grown mid-read: no update past record {record:,}")
        else:
            at = watcher.made_at
            details.append(
                f"{grown.name} (record {record:,}) grown once the read reported record "
                f"{at.done:,} of {at.total:,}"
            )
        return self._sizes(node, 0, not mft_read.incremental and not missed, details)

    def incremental_scan(self):
        for change in KNOWN_CHANGES:
            make_change(self.tracked, change)
        grown = GROWN_DURING_INCREMENTAL
        watcher = ScanWatcher(self.tracked, grown, PHASE_LOADING_CACHE)
        try:
            node, report = scan_with_best_engine(
                self.tracked, watcher, threading.Event(), turbo_enabled=True
            )
        finally:
            missed = watcher.make_change_if_missed()
        if report.engine != ENGINE_TURBO:
            return False, [f"Turbo Scan fell back to Compatible: {report.fallback_reason}"]
        details = [
            f"{report.mft_read.describe()} in {report.elapsed_seconds:.1f}s, after: "
            + "; ".join(describe_change(change) for change in KNOWN_CHANGES),
            f"{grown.name} grown "
            + ("after the scan: it never loaded the cache" if missed else "as it loaded the cache"),
        ]
        return self._sizes(node, 1, report.mft_read.incremental and not missed, details)

    def helper_incremental_scan(self):
        node, mft_read, seconds = self._helper_scan(self.tracked, ScanWatcher())
        compatible = compatible_scan(self.tracked, queue.Queue(), threading.Event())
        differences = _compare(compatible, node)
        details = [f"{mft_read.describe()} in {seconds:.1f}s"]
        details += [f"against Compatible: {d}" for d in differences[:10]] or [
            "matches the Compatible engine on every compared field"
        ]
        return self._sizes(node, 2, mft_read.incremental and not differences, details)

    def compressed_folder(self):
        folder = os.path.join(self.scratch, "compressed")
        write_files(folder, COMPRESSED_FILES)
        run_tool(["compact", "/c", "/s:" + os.path.join(folder, "ntfs"), "/i", "/q"])
        wof_file = os.path.join(folder, "wof", "text.txt")
        run_tool(["compact", "/c", "/exe:xpress4k", "/i", "/q", wof_file])
        passed, details, result = self._engines(folder, since=None)
        for f in (row for node in iter_folders(result.compatible_root) for row in node.files()):
            if f.alloc_size >= f.size:
                passed = False
                details.append(f"{f.path} isn't compressed: {f.alloc_size:,} of {f.size:,} bytes")
        return passed, details

    def _make_junction(self):
        target = os.path.join(self.scratch, "junction_target")
        if not os.path.lexists(self.junction):
            write_files(target, JUNCTION_TARGET_FILES)
            write_files(self.linked, BESIDE_THE_JUNCTION)
            make_junction(self.junction, target)
        if not os.path.lexists(self.file_link):
            # Billed its target's clusters by the Compatible engine until
            # checklist run 5 (a glog .INFO link in Windows\Temp).
            make_file_link(self.file_link, os.path.join(target, "a.bin"))

    def junction_root(self):
        self._make_junction()
        result = compare_path(self.junction, since=None)
        found = result.compatible_root
        expected = sum(JUNCTION_TARGET_FILES.values()), len(JUNCTION_TARGET_FILES)
        details = [
            f"Compatible: {found.size:,} bytes in {found.file_count:,} files "
            f"(the target holds {expected[0]:,} in {expected[1]:,})"
        ]
        if result.turbo_ran:
            turbo = result.turbo_root
            details.append(f"Turbo Scan read it ({turbo.size:,} bytes) instead of falling back")
            return False, details
        reason = result.report.fallback_reason or ""
        details.append(f"Turbo Scan fell back to Compatible: {reason}")
        return (found.size, found.file_count) == expected and "junction" in reason, details

    def junction_parent(self):
        self._make_junction()
        passed, details, result = self._engines(self.linked, since=None)
        if result.turbo_ran:
            turbo_tree = _flatten(result.turbo_root)
            for label, path in (("junction", self.junction), ("file symlink", self.file_link)):
                link = turbo_tree.get(path)
                shown = "missing" if link is None else f"is_link={link.is_link}, size={link.size:,}"
                details.append(f"the {label} in Turbo Scan's tree: {shown}")
        return passed, details

    def _engines(self, path, since):
        """(passed, details, Comparison) for compare_path(path, since). Passed
        when Turbo Scan ran and every difference has a known cause -- or,
        with since=None (a folder only this checklist writes to), when there
        is none."""
        result = compare_path(path, since)
        c = result.compatible_root
        details = [
            f"Compatible: {c.size:,} bytes, {c.alloc_size:,} on disk, {c.file_count:,} files "
            f"in {result.compatible_seconds:.1f}s"
        ]
        if not result.turbo_ran:
            reason = result.report.fallback_reason
            return False, [*details, f"Turbo Scan fell back to Compatible: {reason}"], result
        t, report = result.turbo_root, result.report
        details.append(
            f"Turbo Scan: {t.size:,} bytes, {t.alloc_size:,} on disk, {t.file_count:,} files "
            f"in {report.elapsed_seconds:.1f}s, {report.mft_read.describe()}"
        )
        for category, count in sorted(result.counts().items()):
            examples = [d for d, found in result.classified if found == category]
            details.append(f"{count:,} {category}, e.g. {examples[0]}")
            if category == UNEXPLAINED:
                # Every one, up to a sane limit: each is a finding to look
                # at, and the first 10 alone hid what the rest were.
                details += [f"also {d}" for d in examples[1:200]]
        if not result.classified:
            details.append("no differences")
        passed = not result.classified if since is None else not result.unexplained()
        return passed, details, result


def _git_version():
    try:
        done = subprocess.run(
            ["git", "-C", _HERE, "describe", "--always", "--dirty"], capture_output=True, text=True
        )
    except OSError:
        return "unknown"
    return done.stdout.strip() or "unknown"


def main(argv):
    parser = argparse.ArgumentParser(description="Verify Turbo Scan and its cache (P1-3).")
    parser.add_argument(
        "--results",
        metavar="FILE",
        help="where to save the report (default: turbo-checklist-<date>-<time>.txt here)",
    )
    args = parser.parse_args(argv)
    refusal = refusal_reason()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

    started = datetime.now()
    results_path = os.path.abspath(args.results or f"turbo-checklist-{started:%Y%m%d-%H%M%S}.txt")
    # The long spelling of the path, as Turbo Scan's tree has it (not an
    # 8.3 short name %TEMP% may hold), so the two engines' paths compare.
    scratch = os.path.realpath(tempfile.mkdtemp(prefix="storage-scanner-checklist-"))
    print(f"Scratch folder: {scratch}\nStep 1 reads the whole MFT; steps 7 and 8 take minutes.")
    try:
        results = Checklist(scratch).run()
    finally:
        problem = remove_scratch(scratch)
    environment = (
        f"{platform.platform()}, Python {platform.python_version()}, Storage Scanner "
        f"{_git_version()}, elevated, with a new Turbo Scan cache in a scratch folder."
    )
    report = format_report(started, environment, results, [problem] if problem else [])
    print("\n" + report)
    with open(results_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Saved to {results_path}")
    return 0 if all(result.passed for result in results) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
