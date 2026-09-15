import queue
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import turbo_scan
from storage_scanner.models import Node
from storage_scanner.turbo_scan import ScanReport, choose_engine, find_subtree_node


# -- choose_engine: the full decision matrix -------------------------------- #

def test_turbo_when_windows_enabled_and_ntfs_fixed(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: True)
    assert choose_engine("C:\\Data", turbo_enabled=True) == turbo_scan.ENGINE_TURBO


def test_compatible_when_not_windows(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", False)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: True)
    assert choose_engine("C:\\Data", turbo_enabled=True) == turbo_scan.ENGINE_COMPATIBLE


def test_compatible_when_turbo_disabled(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: True)
    assert choose_engine("C:\\Data", turbo_enabled=False) == turbo_scan.ENGINE_COMPATIBLE


def test_compatible_when_drive_is_not_ntfs_fixed(monkeypatch):
    monkeypatch.setattr(turbo_scan, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_scan, "is_ntfs_fixed_drive", lambda path: False)
    assert choose_engine("E:\\", turbo_enabled=True) == turbo_scan.ENGINE_COMPATIBLE


# -- find_subtree_node ------------------------------------------------------- #

def _make_tree():
    root = Node("C:\\Data", "Data", True)
    docs = Node("C:\\Data\\Docs", "Docs", True)
    photo = Node("C:\\Data\\Docs\\Photo.JPG", "Photo.JPG", False)
    docs.children.append(photo)
    root.children.append(docs)
    return root


def test_find_subtree_node_returns_root_for_the_root_path():
    root = _make_tree()
    assert find_subtree_node(root, "C:\\Data") is root


def test_find_subtree_node_walks_down_to_a_nested_file_case_insensitively():
    root = _make_tree()
    node = find_subtree_node(root, "C:\\Data\\Docs\\photo.jpg")
    assert node.name == "Photo.JPG"


def test_find_subtree_node_raises_for_a_missing_component():
    root = _make_tree()
    with pytest.raises(RuntimeError):
        find_subtree_node(root, "C:\\Data\\DoesNotExist")


# -- scan_with_best_engine ---------------------------------------------------- #

def _progress_and_cancel():
    return queue.Queue(), threading.Event()


def test_uses_compatible_engine_directly_when_turbo_not_applicable(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    fake_node.file_count = 7
    calls = []

    def fake_scan(*args, **kwargs):
        calls.append(1)
        return fake_node

    monkeypatch.setattr(turbo_scan.scanner, "scan", fake_scan)
    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", lambda *a, **k: pytest.fail("should not be called"))

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data", progress_q, cancel_event, turbo_enabled=False,
    )

    assert node is fake_node
    assert report == ScanReport(
        engine=turbo_scan.ENGINE_COMPATIBLE, elapsed_seconds=report.elapsed_seconds,
        file_count=7, fallback_reason=None,
    )
    assert len(calls) == 1


def test_successful_turbo_scan_reports_turbo_engine_and_skips_compatible(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    fake_node.file_count = 123
    monkeypatch.setattr(turbo_scan, "choose_engine", lambda *a, **k: turbo_scan.ENGINE_TURBO)
    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", lambda *a, **k: fake_node)
    monkeypatch.setattr(
        turbo_scan.scanner, "scan",
        lambda *a, **k: pytest.fail("Compatible engine should not run"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data", progress_q, cancel_event, turbo_enabled=True,
    )

    assert node is fake_node
    assert report.engine == turbo_scan.ENGINE_TURBO
    assert report.file_count == 123
    assert report.fallback_reason is None


def test_turbo_failure_falls_back_to_compatible_with_a_reason(monkeypatch):
    fallback_node = Node("C:\\Data", "Data", True)
    fallback_node.file_count = 9

    monkeypatch.setattr(turbo_scan, "choose_engine", lambda *a, **k: turbo_scan.ENGINE_TURBO)

    def boom(*a, **k):
        raise RuntimeError("elevation was declined")

    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", boom)
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: fallback_node)

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data", progress_q, cancel_event, turbo_enabled=True,
    )

    assert node is fallback_node
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE
    assert report.fallback_reason == "elevation was declined"


def test_turbo_disabled_setting_read_from_app_metadata_when_not_passed(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    fake_node.file_count = 1
    monkeypatch.setattr(turbo_scan, "get_app_metadata", lambda key, default: "0")
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: fake_node)
    monkeypatch.setattr(
        turbo_scan, "_attempt_turbo_scan",
        lambda *a, **k: pytest.fail("Turbo Scan should be disabled by app_metadata"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine("C:\\Data", progress_q, cancel_event)

    assert node is fake_node
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE


def test_already_elevated_uses_in_process_path(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)
    called = {}
    def fake_in_process(path, progress_q, cancel_event):
        called["in_process"] = path
        return fake_node

    monkeypatch.setattr(turbo_scan, "_run_turbo_in_process", fake_in_process)
    monkeypatch.setattr(
        turbo_scan, "_run_turbo_via_elevated_helper",
        lambda *a, **k: pytest.fail("should not spawn an elevated helper when already elevated"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    result = turbo_scan._attempt_turbo_scan("C:\\Data", progress_q, cancel_event)

    assert result is fake_node
    assert called["in_process"] == "C:\\Data"


def test_not_elevated_uses_elevated_helper_path(monkeypatch):
    fake_node = Node("C:\\Data", "Data", True)
    monkeypatch.setattr(turbo_scan, "IS_ROOT", False)
    called = {}
    monkeypatch.setattr(
        turbo_scan, "_run_turbo_in_process",
        lambda *a, **k: pytest.fail("should not scan in-process when not elevated"),
    )

    def fake_via_helper(path, cancel_event):
        called["via_helper"] = path
        return fake_node

    monkeypatch.setattr(turbo_scan, "_run_turbo_via_elevated_helper", fake_via_helper)

    progress_q, cancel_event = _progress_and_cancel()
    result = turbo_scan._attempt_turbo_scan("C:\\Data", progress_q, cancel_event)

    assert result is fake_node
    assert called["via_helper"] == "C:\\Data"
