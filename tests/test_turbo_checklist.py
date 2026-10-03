import os
import queue
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import turbo_checklist
from storage_scanner.scan_progress import Phase
from storage_scanner.scanner import scan
from turbo_checklist import StepResult, format_report, main
from turbo_checklist_fixtures import (
    CREATE,
    DELETE,
    GROW,
    GROWN_DURING_FULL_READ,
    GROWN_DURING_INCREMENTAL,
    KNOWN_CHANGES,
    RENAME,
    TRACKED_FILES,
    Change,
    ScanWatcher,
    apply_changes,
    expected_sizes,
    file_sizes,
    make_change,
    make_junction,
    remove_scratch,
    size_problems,
    write_files,
)

MFT = "Reading the MFT"


def test_apply_changes_creates_grows_moves_and_deletes():
    files = {"a": 1, "b": 2, "c": 3}
    changes = [
        Change(CREATE, "d", size=4),
        Change(GROW, "a", size=10),
        Change(RENAME, "b", new_name=os.path.join("sub", "b2")),
        Change(DELETE, "c"),
    ]

    assert apply_changes(files, changes) == {"a": 10, os.path.join("sub", "b2"): 2, "d": 4}
    assert files == {"a": 1, "b": 2, "c": 3}


def test_the_planned_changes_made_on_disk_leave_what_the_plan_predicts(tmp_path):
    write_files(str(tmp_path), TRACKED_FILES)
    for change in KNOWN_CHANGES:
        make_change(str(tmp_path), change)

    root = scan(str(tmp_path), queue.Queue(), threading.Event())

    assert file_sizes(root) == apply_changes(TRACKED_FILES, KNOWN_CHANGES)


def test_a_file_grown_during_a_scan_may_show_either_size_then_only_the_new_one():
    full_read, incremental, last = expected_sizes()
    during_full, during_incremental = GROWN_DURING_FULL_READ, GROWN_DURING_INCREMENTAL
    old_full, old_incremental = (
        TRACKED_FILES[during_full.name],
        TRACKED_FILES[during_incremental.name],
    )

    assert full_read[during_full.name] == (old_full, during_full.size)
    assert incremental[during_full.name] == last[during_full.name] == (during_full.size,)
    assert incremental[during_incremental.name] == (old_incremental, during_incremental.size)
    assert last[during_incremental.name] == (during_incremental.size,)
    assert set(incremental) == set(apply_changes(TRACKED_FILES, KNOWN_CHANGES))


def test_size_problems_names_each_missing_extra_and_wrong_file():
    actual = {"a": 1, "b": 5, "x": 9}
    expected = {"a": (1,), "b": (2, 3), "c": (4,)}

    assert size_problems(actual, expected) == [
        "c: missing",
        "x: shouldn't be there",
        "b: 5 bytes, expected 2 or 3",
    ]
    assert size_problems({"b": 3}, {"b": (2, 3)}) == []


def test_the_watcher_makes_its_change_once_the_scan_is_past_the_record(tmp_path):
    write_files(str(tmp_path), {"f.bin": 10})
    watcher = ScanWatcher(str(tmp_path), Change(GROW, "f.bin", size=50), MFT, after=100)
    grown = tmp_path / "f.bin"

    watcher.put(("walk", None))
    watcher.put(("phase", Phase(MFT, 100, 1000)))
    watcher.put(("phase", Phase("Saving the Turbo Scan cache", 500, 1000)))
    assert grown.stat().st_size == 10

    watcher.put(("phase", Phase(MFT, 101, 1000)))
    watcher.put(("phase", Phase(MFT, 900, 1000)))

    assert grown.stat().st_size == 50
    assert watcher.made_at == Phase(MFT, 101, 1000)
    assert len(watcher.phases) == 4
    assert watcher.make_change_if_missed() is False


def test_a_change_the_scan_never_reached_is_made_afterwards(tmp_path):
    write_files(str(tmp_path), {"f.bin": 10})
    watcher = ScanWatcher(str(tmp_path), Change(GROW, "f.bin", size=50), MFT, after=100)

    watcher.put(("phase", Phase("Loading cached records")))

    assert watcher.make_change_if_missed() is True
    assert (tmp_path / "f.bin").stat().st_size == 50
    assert watcher.made_at is None


def test_the_report_counts_passed_steps_and_lists_each_ones_details():
    results = [
        StepResult("Full scan", True, ["Full (first scan of this drive) in 41.0s"]),
        StepResult("C:\\Windows", False, ["3 unexplained, e.g. x"]),
    ]

    report = format_report(datetime(2026, 10, 3, 14, 2), "Windows-11", results, ["kept X"])

    assert report.splitlines() == [
        "Turbo Scan checklist (P1-3), 2026-10-03 14:02: 1 of 2 steps passed.",
        "Windows-11",
        "",
        "- PASS 1. Full scan",
        "  - Full (first scan of this drive) in 41.0s",
        "- FAIL 2. C:\\Windows",
        "  - 3 unexplained, e.g. x",
        "Note: kept X",
    ]


def test_refuses_when_not_elevated_before_creating_anything(monkeypatch, tmp_path, capsys):
    def no_scratch(*_args, **_kwargs):
        raise AssertionError("created a scratch folder")

    monkeypatch.setattr(turbo_checklist, "IS_WINDOWS", True)
    monkeypatch.setattr(turbo_checklist, "IS_ROOT", False)
    monkeypatch.setattr(tempfile, "mkdtemp", no_scratch)
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1

    message = capsys.readouterr().err
    assert "elevated" in message
    assert "turbo_checklist.py" in message
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_removing_the_scratch_folder_leaves_what_a_junction_in_it_points_to(tmp_path):
    scratch, outside = tmp_path / "scratch", tmp_path / "outside"
    write_files(str(outside), {"keep.bin": 10})
    write_files(str(scratch), {"own.bin": 10})
    make_junction(str(scratch / "link"), str(outside))

    assert remove_scratch(str(scratch)) is None

    assert not scratch.exists()
    assert (outside / "keep.bin").stat().st_size == 10
