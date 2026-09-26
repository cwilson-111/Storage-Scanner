import queue
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import turbo_scan
from storage_scanner.models import Node
from storage_scanner.serialization import node_to_dict
from storage_scanner.turbo_read import MftRead
from storage_scanner.turbo_scan import ScanReport, choose_engine, scan_indicators

INCREMENTAL = MftRead(incremental=True)

# -- scan_indicators: what the scan-details strip shows ---------------------- #


def test_indicators_turbo_scan_complete():
    report = ScanReport(engine=turbo_scan.ENGINE_TURBO, elapsed_seconds=2.0, file_count=10_000)
    fields, complete = scan_indicators(report, 0)
    assert complete
    assert dict(fields) == {
        "Engine": "Turbo Scan (NTFS MFT)",
        "Elapsed": "2.0s",
        "Throughput": "5,000 files/s",
        "Unreadable paths": "0",
        "Result": "Complete",
    }


def test_indicators_fallback_is_named_and_unreadable_paths_mark_incomplete():
    report = ScanReport(
        engine=turbo_scan.ENGINE_COMPATIBLE,
        elapsed_seconds=1.0,
        file_count=50,
        fallback_reason="elevation was declined",
    )
    fields, complete = scan_indicators(report, 3)
    assert not complete
    assert dict(fields)["Engine"] == "Compatible (Turbo Scan fell back)"
    assert dict(fields)["Unreadable paths"] == "3"
    assert dict(fields)["Result"] == "Incomplete (some paths unreadable)"


def test_indicators_zero_elapsed_does_not_divide_by_zero():
    report = ScanReport(engine=turbo_scan.ENGINE_COMPATIBLE, elapsed_seconds=0.0, file_count=1)
    fields, _ = scan_indicators(report, 0)
    assert dict(fields)["Throughput"] == "—"


def test_indicators_without_report_from_elevated_helper():
    fields, complete = scan_indicators(None, 0)
    assert complete
    assert dict(fields)["Engine"] == "Compatible (elevated helper)"
    assert dict(fields)["Elapsed"] == "—"
    assert dict(fields)["Throughput"] == "—"


def test_indicators_say_how_a_turbo_scan_read_the_mft():
    def mft_field(mft_read):
        report = ScanReport(
            engine=turbo_scan.ENGINE_TURBO,
            elapsed_seconds=1.0,
            file_count=1,
            mft_read=mft_read,
        )
        fields, _ = scan_indicators(report, 0)
        assert [label for label, _ in fields][:2] == ["Engine", "MFT read"]
        return dict(fields)["MFT read"]

    assert mft_field(INCREMENTAL) == "Incremental (USN journal)"
    assert (
        mft_field(MftRead(incremental=False, full_read_reason="first scan of this drive"))
        == "Full (first scan of this drive)"
    )


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


# -- scan_with_best_engine ---------------------------------------------------- #


def _progress_and_cancel():
    return queue.Queue(), threading.Event()


def test_uses_compatible_engine_directly_when_turbo_not_applicable(monkeypatch):
    fake_node = Node("C:\\Data", "Data")
    fake_node.file_count = 7
    calls = []

    def fake_scan(*args, **kwargs):
        calls.append(1)
        return fake_node

    monkeypatch.setattr(turbo_scan.scanner, "scan", fake_scan)
    monkeypatch.setattr(
        turbo_scan, "_attempt_turbo_scan", lambda *a, **k: pytest.fail("should not be called")
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data",
        progress_q,
        cancel_event,
        turbo_enabled=False,
    )

    assert node is fake_node
    assert report == ScanReport(
        engine=turbo_scan.ENGINE_COMPATIBLE,
        elapsed_seconds=report.elapsed_seconds,
        file_count=7,
        fallback_reason=None,
    )
    assert len(calls) == 1


def test_successful_turbo_scan_reports_turbo_engine_and_skips_compatible(monkeypatch):
    fake_node = Node("C:\\Data", "Data")
    fake_node.file_count = 123
    monkeypatch.setattr(turbo_scan, "choose_engine", lambda *a, **k: turbo_scan.ENGINE_TURBO)
    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", lambda *a, **k: (fake_node, INCREMENTAL))
    monkeypatch.setattr(
        turbo_scan.scanner,
        "scan",
        lambda *a, **k: pytest.fail("Compatible engine should not run"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert node is fake_node
    assert report.engine == turbo_scan.ENGINE_TURBO
    assert report.file_count == 123
    assert report.fallback_reason is None
    assert report.mft_read == INCREMENTAL


def test_turbo_failure_falls_back_to_compatible_with_a_reason(monkeypatch):
    fallback_node = Node("C:\\Data", "Data")
    fallback_node.file_count = 9

    monkeypatch.setattr(turbo_scan, "choose_engine", lambda *a, **k: turbo_scan.ENGINE_TURBO)

    def boom(*a, **k):
        raise RuntimeError("elevation was declined")

    monkeypatch.setattr(turbo_scan, "_attempt_turbo_scan", boom)
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: fallback_node)

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine(
        "C:\\Data",
        progress_q,
        cancel_event,
        turbo_enabled=True,
    )

    assert node is fallback_node
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE
    assert report.fallback_reason == "elevation was declined"


def test_turbo_disabled_setting_read_from_app_metadata_when_not_passed(monkeypatch):
    fake_node = Node("C:\\Data", "Data")
    fake_node.file_count = 1
    monkeypatch.setattr(turbo_scan, "get_app_metadata", lambda key, default: "0")
    monkeypatch.setattr(turbo_scan.scanner, "scan", lambda *a, **k: fake_node)
    monkeypatch.setattr(
        turbo_scan,
        "_attempt_turbo_scan",
        lambda *a, **k: pytest.fail("Turbo Scan should be disabled by app_metadata"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    node, report = turbo_scan.scan_with_best_engine("C:\\Data", progress_q, cancel_event)

    assert node is fake_node
    assert report.engine == turbo_scan.ENGINE_COMPATIBLE


def test_already_elevated_uses_in_process_path(monkeypatch):
    fake_node = Node("C:\\Data", "Data")
    monkeypatch.setattr(turbo_scan, "IS_ROOT", True)
    called = {}

    def fake_in_process(path, progress_q, cancel_event):
        called["in_process"] = path
        return fake_node, INCREMENTAL

    monkeypatch.setattr(turbo_scan, "_run_turbo_in_process", fake_in_process)
    monkeypatch.setattr(
        turbo_scan,
        "_run_turbo_via_elevated_helper",
        lambda *a, **k: pytest.fail("should not spawn an elevated helper when already elevated"),
    )

    progress_q, cancel_event = _progress_and_cancel()
    result = turbo_scan._attempt_turbo_scan("C:\\Data", progress_q, cancel_event)

    assert result == (fake_node, INCREMENTAL)
    assert called["in_process"] == "C:\\Data"


def test_not_elevated_uses_elevated_helper_path(monkeypatch):
    fake_node = Node("C:\\Data", "Data")
    monkeypatch.setattr(turbo_scan, "IS_ROOT", False)
    called = {}
    monkeypatch.setattr(
        turbo_scan,
        "_run_turbo_in_process",
        lambda *a, **k: pytest.fail("should not scan in-process when not elevated"),
    )

    def fake_via_helper(path, progress_q, cancel_event):
        called["via_helper"] = path
        return fake_node, INCREMENTAL

    monkeypatch.setattr(turbo_scan, "_run_turbo_via_elevated_helper", fake_via_helper)

    progress_q, cancel_event = _progress_and_cancel()
    result = turbo_scan._attempt_turbo_scan("C:\\Data", progress_q, cancel_event)

    assert result == (fake_node, INCREMENTAL)
    assert called["via_helper"] == "C:\\Data"


def test_elevated_helper_result_carries_the_node_and_how_the_mft_was_read(monkeypatch):
    # The helper runs in another process; this is the far side of the
    # envelope mft_scan_cli.run_mft_scan writes.
    node = Node("C:\\Data", "Data")
    node.file_count = 4
    mft_read = MftRead(incremental=False, full_read_reason="USN journal was recreated")
    monkeypatch.setattr(
        turbo_scan,
        "run_elevated_scan_windows",
        lambda *a: (True, {"node": node_to_dict(node), "mft_read": mft_read.to_dict()}),
    )

    result_node, result_read = turbo_scan._run_turbo_via_elevated_helper("C:\\Data", None, None)

    assert (result_node.path, result_node.file_count) == ("C:\\Data", 4)
    assert result_read == mft_read
