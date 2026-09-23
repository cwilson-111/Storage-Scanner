import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner import scan_history
from storage_scanner.models import Node

MB = 1024 * 1024


def _db(tmp_path, monkeypatch):
    # Deliberately NOT calling history.init_history_db() here: record_scan
    # must create the tables itself, because the CLI never runs the GUI's
    # startup that normally does it.
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))


def _tree(root_path, big_size=60 * MB, small_size=1 * MB):
    root = Node(root_path, "root", True)
    big = Node(os.path.join(root_path, "big"), "big", True)
    small = Node(os.path.join(root_path, "small"), "small", True)
    big.size, big.file_count = big_size, 3
    small.size, small.file_count = small_size, 2
    root.children = [big, small]
    root.size = big.size + small.size
    root.file_count = 5
    return root


def test_collect_folder_sizes_keeps_the_root_and_big_folders_but_counts_every_folder(tmp_path):
    root = _tree(str(tmp_path))

    folder_sizes, folder_count = scan_history.collect_folder_sizes(root)

    assert folder_count == 3
    assert set(folder_sizes) == {root.path, os.path.join(str(tmp_path), "big")}


def test_record_scan_works_on_a_database_that_was_never_initialized(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    recorded = scan_history.record_scan(_tree(str(tmp_path)))

    assert recorded.scan_id
    assert recorded.previous_scan_id is None
    assert recorded.growth_rows == []
    scan_path = scan_history.normalize_scan_path(str(tmp_path))
    assert history.get_latest_scan_id(scan_path) == recorded.scan_id


def test_second_scan_of_the_same_path_reports_growth_against_the_first(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    first = scan_history.record_scan(_tree(str(tmp_path), big_size=60 * MB))

    # A trailing separator is a different spelling of the same folder.
    second = scan_history.record_scan(_tree(str(tmp_path) + os.sep, big_size=90 * MB))

    assert second.previous_scan_id == first.scan_id
    assert second.growth_rows


def test_record_scan_reports_a_budget_breach(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    history.init_history_db()
    history.set_budget(scan_history.normalize_scan_path(str(tmp_path)), 10 * MB)

    recorded = scan_history.record_scan(_tree(str(tmp_path)))

    assert recorded.budget_breach is not None
    assert recorded.budget_breach.threshold_bytes == 10 * MB
