"""Chooses between the Compatible (directory-walking) and Turbo (NTFS MFT)
scan engines, and safely falls back to Compatible on any Turbo failure.

`_run_turbo_in_process`/`_run_turbo_via_elevated_helper` are the two real
seams into the rest of Turbo Scan (storage_scanner.mft_volume's raw-volume
reader, and storage_scanner.file_ops.run_elevated_scan_windows's headless
elevated helper, respectively). Whatever goes wrong in either -- a denied
elevation prompt, a raw-volume read error, a single unparseable MFT
record, a missing subtree path, anything at all -- is just another Turbo
failure to scan_with_best_engine()'s broad except: it always falls back to
the Compatible engine rather than crash or hang.

Reading the volume itself -- from the cache refreshed via the USN journal,
or a full MFT read -- is storage_scanner.turbo_read, shared with the
elevated helper (mft_scan_cli.py). It reports which path it took
(MftRead), for the scan-details strip.
"""

import time
from dataclasses import dataclass
from typing import Optional

from history import get_app_metadata
from storage_scanner import mft_volume, scanner
from storage_scanner.drive_info import get_volume_root, is_ntfs_fixed_drive
from storage_scanner.file_ops import run_elevated_scan_windows
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_ROOT, IS_WINDOWS
from storage_scanner.serialization import dict_to_node
from storage_scanner.turbo_read import MftRead, scan_subtree_using_cache

ENGINE_TURBO = "turbo"
ENGINE_COMPATIBLE = "compatible"


@dataclass
class ScanReport:
    """Travels alongside the Node returned by scan_with_best_engine(),
    never folded into Node itself -- storage_scanner.scanner.scan() and
    its existing callers (cli.py, priv_scan_cli.py, tests/test_scan.py)
    stay completely unaware Turbo Scan exists."""

    engine: str
    elapsed_seconds: float
    file_count: int
    fallback_reason: Optional[str] = None
    mft_read: Optional[MftRead] = None  # Turbo Scan only


def scan_indicators(report, unreadable_count):
    """What the main window's scan-details strip shows after a scan:
    ([(label, value), ...], complete).

    `report` is None when the scan ran through the macOS/Linux elevated
    helper, which hands back only a tree with no timing. A scan counts as
    complete only when no path was unreadable: a Turbo Scan never returns a
    partial tree (any failure falls back, see scan_with_best_engine), so
    unreadable paths are the only way a finished scan can be missing data.
    """
    if report is None:
        engine = "Compatible (elevated helper)"
        elapsed = throughput = "—"
    else:
        engine = "Turbo Scan (NTFS MFT)" if report.engine == ENGINE_TURBO else "Compatible"
        if report.fallback_reason:
            engine += " (Turbo Scan fell back)"
        elapsed = f"{report.elapsed_seconds:.1f}s"
        throughput = (
            f"{report.file_count / report.elapsed_seconds:,.0f} files/s"
            if report.elapsed_seconds > 0
            else "—"
        )
    complete = unreadable_count == 0
    result = "Complete" if complete else "Incomplete (some paths unreadable)"
    fields = [("Engine", engine)]
    if report is not None and report.mft_read is not None:
        fields.append(("MFT read", report.mft_read.describe()))
    fields += [
        ("Elapsed", elapsed),
        ("Throughput", throughput),
        ("Unreadable paths", f"{unreadable_count:,}"),
        ("Result", result),
    ]
    return fields, complete


def choose_engine(path, turbo_enabled):
    """ "turbo" only on Windows, only when the caller says Turbo Scan is
    enabled, and only on a local fixed NTFS volume -- "compatible"
    otherwise. Never raises (is_ntfs_fixed_drive doesn't either)."""
    if not IS_WINDOWS or not turbo_enabled:
        return ENGINE_COMPATIBLE
    if not is_ntfs_fixed_drive(path):
        return ENGINE_COMPATIBLE
    return ENGINE_TURBO


def _run_turbo_in_process(path, progress_q, cancel_event):
    """Already elevated: read the volume and build the tree in this same
    process, skipping the subprocess bridge entirely. Returns (Node, MftRead)."""
    volume_root = get_volume_root(path)
    record_source = mft_volume.open_record_source(volume_root)
    try:
        return scan_subtree_using_cache(record_source, volume_root, path, progress_q, cancel_event)
    finally:
        record_source.close()


def _run_turbo_via_elevated_helper(path, progress_q, cancel_event):
    """Not yet elevated: hand the raw-volume read off to a headless
    elevated helper process and reconstruct its result. The helper's own
    `--subtree` handling already resolves the subtree (turbo_read)
    before ever serializing, so `result["node"]` on success is that
    subtree's dict, ready for dict_to_node, and `result["mft_read"]` is the
    helper's MftRead -- see storage_scanner/mft_scan_cli.py. Returns
    (Node, MftRead).

    `progress_q` is relayed live record counts from the elevated process
    via a polled progress file -- see run_elevated_scan_windows's and
    mft_scan_cli._ProgressFileWriter's docstrings for why a real queue
    can't just be shared across the process boundary directly.
    """
    ok, result = run_elevated_scan_windows(path, progress_q, cancel_event)
    if not ok:
        raise RuntimeError(result)
    return dict_to_node(result["node"]), MftRead.from_dict(result["mft_read"])


def _attempt_turbo_scan(path, progress_q, cancel_event):
    """(Node, MftRead) from whichever Turbo path fits this process."""
    if IS_ROOT:
        return _run_turbo_in_process(path, progress_q, cancel_event)
    return _run_turbo_via_elevated_helper(path, progress_q, cancel_event)


def scan_with_best_engine(path, progress_q, cancel_event, workers=None, turbo_enabled=None):
    """Drop-in richer replacement for storage_scanner.scanner.scan(): same
    progress_q/cancel_event contract, but returns (Node, ScanReport)
    instead of just a Node.

    Falls back to the Compatible engine (scanner.scan(), called completely
    unchanged) whenever Turbo Scan isn't applicable, and on ANY Turbo
    failure whatsoever -- a declined elevation prompt, a raw-volume read
    error, a single unparseable MFT record, a missing subtree path,
    anything. Turbo Scan aborts its whole attempt rather than ever
    returning a partial tree on error: a visible, harmless fallback is
    safer than silently presenting an incomplete scan as if it were
    complete.
    """
    if turbo_enabled is None:
        turbo_enabled = get_app_metadata("turbo_scan_enabled", "0") == "1"

    engine = choose_engine(path, turbo_enabled)
    fallback_reason = None

    if engine == ENGINE_TURBO:
        start = time.perf_counter()
        try:
            root_node, mft_read = _attempt_turbo_scan(path, progress_q, cancel_event)
        except Exception as exc:  # noqa: BLE001 - any Turbo failure falls back
            logger.warning(
                "Turbo Scan of %r failed, falling back to Compatible Scan: %s",
                path,
                exc,
                exc_info=True,
            )
            fallback_reason = str(exc) or exc.__class__.__name__
        else:
            elapsed = time.perf_counter() - start
            return root_node, ScanReport(
                engine=ENGINE_TURBO,
                elapsed_seconds=elapsed,
                file_count=root_node.file_count,
                fallback_reason=None,
                mft_read=mft_read,
            )

    start = time.perf_counter()
    root_node = scanner.scan(path, progress_q, cancel_event, workers=workers)
    elapsed = time.perf_counter() - start
    return root_node, ScanReport(
        engine=ENGINE_COMPATIBLE,
        elapsed_seconds=elapsed,
        file_count=root_node.file_count,
        fallback_reason=fallback_reason,
    )
