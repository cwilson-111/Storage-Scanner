"""Records a finished scan into scan history — shared by the GUI (after every
scan) and the headless CLI (`--cli <path> --save-history`, which is what a
scheduled scan runs). Kept free of Tk so both can call it.
"""

import os
import shutil
from dataclasses import dataclass

import history
from storage_scanner.budgets import check_budget_for_path
from storage_scanner.logging_setup import logger

# Only folders at least this big (plus the scan root) get a history row, so
# a huge scan doesn't write millions of rows nobody will chart.
MIN_FOLDER_SIZE_FOR_HISTORY = 50 * 1024 * 1024


@dataclass
class RecordedScan:
    scan_id: int
    previous_scan_id: int  # None on the first scan of a path
    growth_rows: list
    budget_breach: object  # budgets.BudgetBreach, or None


def normalize_scan_path(path):
    """The key scan history is stored under, so C:\\Data and c:\\data\\ match."""
    return os.path.normcase(os.path.normpath(path))


def collect_folder_sizes(root_node, min_size=MIN_FOLDER_SIZE_FOR_HISTORY):
    """Folders worth a history row ({path: {"size", "file_count"}}), and the
    total folder count of the whole tree."""
    folder_sizes = {}
    folder_count = 0
    stack = [root_node]

    while stack:
        node = stack.pop()

        if node.is_dir:
            folder_count += 1

            if node.size >= min_size or node is root_node:
                folder_sizes[node.path] = {
                    "size": node.size,
                    "file_count": node.file_count,
                }

            stack.extend(node.children)

    return folder_sizes, folder_count


def drive_capacity_bytes(path):
    """Total capacity of the drive holding path, or 0 if it can't be read."""
    try:
        return shutil.disk_usage(path).total
    except Exception:
        logger.warning("disk_usage(%r) failed", path, exc_info=True)
        return 0


def record_scan(node, growth_limit=50):
    """Saves one finished scan and returns a RecordedScan with its growth
    against the previous scan of the same path. Creates the history tables
    first if needed (idempotent): the CLI never goes through the GUI's own
    startup, so nothing else would."""
    history.init_history_db()

    folder_sizes, folder_count = collect_folder_sizes(node)
    scan_path = normalize_scan_path(node.path)

    scan_id = history.save_scan_snapshot(
        scan_path=scan_path,
        total_size=node.size,
        drive_capacity=drive_capacity_bytes(node.path),
        file_count=node.file_count,
        folder_count=folder_count,
        folder_sizes=folder_sizes,
    )
    previous_scan_id = history.get_previous_scan_id(
        scan_path=scan_path, current_scan_id=scan_id,
    )
    growth_rows = (
        history.get_folder_growth(
            current_scan_id=scan_id, previous_scan_id=previous_scan_id, limit=growth_limit,
        )
        if previous_scan_id else []
    )

    return RecordedScan(
        scan_id=scan_id,
        previous_scan_id=previous_scan_id,
        growth_rows=growth_rows,
        budget_breach=check_budget_for_path(scan_path, node.size),
    )
