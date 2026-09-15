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
"""

import os
import time
from dataclasses import dataclass

from history import get_app_metadata
from storage_scanner import mft_parser, mft_scan, mft_volume, scanner
from storage_scanner.drive_info import get_volume_root, is_ntfs_fixed_drive
from storage_scanner.file_ops import run_elevated_scan_windows
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_ROOT, IS_WINDOWS
from storage_scanner.serialization import dict_to_node

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
    fallback_reason: str = None


def choose_engine(path, turbo_enabled):
    """"turbo" only on Windows, only when the caller says Turbo Scan is
    enabled, and only on a local fixed NTFS volume -- "compatible"
    otherwise. Never raises (is_ntfs_fixed_drive doesn't either)."""
    if not IS_WINDOWS or not turbo_enabled:
        return ENGINE_COMPATIBLE
    if not is_ntfs_fixed_drive(path):
        return ENGINE_COMPATIBLE
    return ENGINE_TURBO


def find_subtree_node(root_node, target_path):
    """Walk down from `root_node` (built for an entire volume) to the Node
    matching `target_path` exactly.

    Turbo Scan always reads a whole volume's MFT -- its records are stored
    in arbitrary order, so there's no way to know which ones live under a
    requested subfolder without reading all of them first -- so both the
    in-process path here and the future elevated-helper CLI's `--subtree`
    flag need this same "slice a subtree back out of the full tree" step.

    Raises if any path component isn't found in the tree (a real path that
    isn't reachable in the scanned tree signals something is wrong, not a
    case to silently paper over) -- sending the caller to the fallback
    engine rather than trust a wrong result.
    """
    target_path = os.path.abspath(target_path)
    relative = os.path.relpath(target_path, root_node.path)

    node = root_node
    for part in relative.split(os.sep):
        if not part or part == os.curdir:
            continue
        match = next(
            (c for c in node.children if os.path.normcase(c.name) == os.path.normcase(part)),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"Turbo Scan could not locate {target_path!r} in the volume tree"
            )
        node = match
    return node


def _run_turbo_in_process(path, progress_q, cancel_event):
    """Already elevated: read the volume and build the tree in this same
    process, skipping the subprocess bridge entirely."""
    volume_root = get_volume_root(path)
    record_source = mft_volume.open_record_source(volume_root)
    try:
        records = []
        for record_number in range(record_source.record_count):
            if cancel_event.is_set():
                break
            parsed = mft_parser.parse_base_record(record_number, record_source)
            if parsed is not None:
                records.append(parsed)
                if len(records) % 5000 == 0:
                    progress_q.put(("progress", len(records)))
    finally:
        record_source.close()

    root_node, orphan_count = mft_scan.build_tree(records, root_path=volume_root)
    if root_node is None:
        raise RuntimeError("Turbo Scan could not locate a root directory record")
    if orphan_count:
        logger.warning("Turbo Scan of %r had %d unreachable record(s)", path, orphan_count)
    return find_subtree_node(root_node, path)


def _run_turbo_via_elevated_helper(path, cancel_event):
    """Not yet elevated: hand the raw-volume read off to a headless
    elevated helper process and reconstruct its result. The helper's own
    `--subtree` handling already resolves the subtree via find_subtree_node
    before ever serializing, so `result` on success is that subtree's dict,
    ready for dict_to_node -- see storage_scanner/mft_scan_cli.py.
    """
    ok, result = run_elevated_scan_windows(path, cancel_event)
    if not ok:
        raise RuntimeError(result)
    return dict_to_node(result)


def _attempt_turbo_scan(path, progress_q, cancel_event):
    if IS_ROOT:
        return _run_turbo_in_process(path, progress_q, cancel_event)
    return _run_turbo_via_elevated_helper(path, cancel_event)


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
            root_node = _attempt_turbo_scan(path, progress_q, cancel_event)
        except Exception as exc:  # noqa: BLE001 - any Turbo failure falls back
            logger.warning(
                "Turbo Scan of %r failed, falling back to Compatible Scan: %s",
                path, exc, exc_info=True,
            )
            fallback_reason = str(exc) or exc.__class__.__name__
        else:
            elapsed = time.perf_counter() - start
            return root_node, ScanReport(
                engine=ENGINE_TURBO, elapsed_seconds=elapsed,
                file_count=root_node.file_count, fallback_reason=None,
            )

    start = time.perf_counter()
    root_node = scanner.scan(path, progress_q, cancel_event, workers=workers)
    elapsed = time.perf_counter() - start
    return root_node, ScanReport(
        engine=ENGINE_COMPATIBLE, elapsed_seconds=elapsed,
        file_count=root_node.file_count, fallback_reason=fallback_reason,
    )
