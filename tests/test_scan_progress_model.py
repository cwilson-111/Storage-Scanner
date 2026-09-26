"""Tests for storage_scanner.scan_progress_model: which estimate a scan's
progress is measured against, and what the progress panel shows from the
scan's messages -- the overall bar, per-folder bars, and the finished,
cancelled and failed states.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner.models import Node
from storage_scanner.scan_history import record_scan
from storage_scanner.scan_progress import (
    DONE,
    QUEUED,
    SCANNING,
    FolderProgress,
    Phase,
    WalkSnapshot,
)
from storage_scanner.scan_progress_model import (
    CANCELLED,
    ESTIMATE_DRIVE_USED,
    ESTIMATE_LAST_SCAN,
    FAILED,
    FINISHED,
    RUNNING_FRACTION_CAP,
    ScanEstimate,
    ScanProgressModel,
    choose_estimate,
    load_estimate,
)

MB = 1024 * 1024
TARGET = os.path.join(os.path.abspath(os.sep), "Data")
LAST_SCAN = ("2026-09-24T10:00:00", 5000, 400, 12)  # created_at, bytes, files, folders


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _walk(files=0, nbytes=0, top_folders=(), more_folders=None, folders=1):
    return WalkSnapshot(
        files=files,
        bytes=nbytes,
        folders=folders,
        pending=1,
        current_path=None,
        current_seconds=0.0,
        current_entries=0,
        top_folders=tuple(top_folders),
        more_folders=more_folders,
    )


def _model(estimate=None, clock=None):
    model = ScanProgressModel(TARGET, clock=clock or _Clock())
    model.handle("estimate", estimate)
    return model


# -- choosing an estimate ------------------------------------------------------ #


def test_the_last_scan_of_the_same_path_wins_over_the_drives_used_space():
    estimate = choose_estimate(TARGET, LAST_SCAN, [], volume_used_bytes=10**12)

    assert (estimate.source, estimate.total_bytes, estimate.total_files) == (
        ESTIMATE_LAST_SCAN,
        5000,
        400,
    )


def test_a_volume_root_never_scanned_is_measured_against_its_used_space():
    estimate = choose_estimate(TARGET, None, [], volume_used_bytes=123_456)

    assert (estimate.source, estimate.total_bytes, estimate.total_files) == (
        ESTIMATE_DRIVE_USED,
        123_456,
        None,
    )


def test_an_empty_last_scan_is_no_estimate():
    empty = ("2026-09-24T10:00:00", 0, 0, 1)
    assert choose_estimate(TARGET, empty, [], volume_used_bytes=None) is None
    assert choose_estimate(TARGET, empty, [], volume_used_bytes=99).source == ESTIMATE_DRIVE_USED
    assert choose_estimate(TARGET, None, [], volume_used_bytes=None) is None


def test_only_the_targets_direct_children_keep_their_last_file_count():
    rows = [
        (TARGET, 5000),
        (os.path.join(TARGET, "Docs"), 3000),
        (os.path.join(TARGET, "Docs", "Deep"), 2000),
        (os.path.join(TARGET, "Media"), 1500),
        (TARGET + "2", 900),  # a sibling that merely shares the prefix
        (os.path.join(TARGET + "2", "Other"), 800),
    ]

    estimate = choose_estimate(TARGET, LAST_SCAN, rows, volume_used_bytes=None)

    assert estimate.folder_files == {
        os.path.normcase("Docs"): 3000,
        os.path.normcase("Media"): 1500,
    }


def test_load_estimate_reads_the_last_saved_scan_and_its_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    root_path = str(tmp_path / "scanned")
    root = Node(root_path, "scanned", True)
    big = Node(os.path.join(root_path, "Big"), "Big", True)
    big.size, big.file_count = 80 * MB, 30
    small = Node(os.path.join(root_path, "small"), "small", True)
    small.size, small.file_count = 1 * MB, 3
    root.children = [big, small]
    root.size, root.file_count = big.size + small.size, 33
    record_scan(root)

    estimate = load_estimate(root_path)

    assert (estimate.source, estimate.total_bytes, estimate.total_files) == (
        ESTIMATE_LAST_SCAN,
        81 * MB,
        33,
    )
    # History keeps folders of 50 MB and up, so only Big has a prior count.
    assert estimate.folder_files == {os.path.normcase("Big"): 30}


def test_load_estimate_without_history_or_a_volume_root_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()

    assert load_estimate(str(tmp_path)) is None


# -- the overall bar ----------------------------------------------------------- #


def test_the_bar_follows_files_against_the_expected_count_and_never_fills_while_running():
    model = _model(ScanEstimate(total_bytes=10**9, total_files=1000, source=ESTIMATE_LAST_SCAN))

    model.handle("walk", _walk(files=250, nbytes=900_000_000))
    assert model.fraction() == 0.25  # files, not bytes, when the file count is known
    assert model.view().percent == "25%"

    model.handle("walk", _walk(files=5000))  # the folder grew since the last scan
    assert model.fraction() == RUNNING_FRACTION_CAP
    assert model.view().percent == "99%"


def test_the_bar_follows_bytes_when_only_the_drives_used_space_is_known():
    model = _model(ScanEstimate(total_bytes=1000, total_files=None, source=ESTIMATE_DRIVE_USED))

    model.handle("walk", _walk(files=10, nbytes=250))

    assert model.fraction() == 0.25


def test_without_an_estimate_the_bar_animates_until_a_counted_step_arrives():
    model = _model(None)
    model.handle("walk", _walk(files=10, nbytes=250))
    assert model.fraction() is None
    assert model.view().percent == ""

    model.handle("phase", Phase("Reading the MFT", 250, 1000, "records"))
    assert model.fraction() == 0.25
    model.handle("phase", Phase("Reading the MFT", 1200, 1000, "records"))
    assert model.fraction() == 1.0
    model.handle("phase", Phase("Saving the Turbo Scan cache"))
    assert model.fraction() is None
    model.handle("phase", Phase("Reading the MFT", 0, 0, "records"))  # an empty volume
    assert model.fraction() is None


def test_a_walk_after_turbo_steps_is_timed_from_when_the_walk_started():
    # A Turbo Scan that falls back already spent time on its own steps;
    # the walk's files/s must not be diluted by them.
    clock = _Clock()
    model = _model(None, clock)
    model.handle("phase", Phase("Reading the MFT", 10, 100, "records"))
    clock.now += 10
    model.handle("walk", _walk(files=0))
    clock.now += 2
    model.handle("walk", _walk(files=1000))

    view = model.view()
    assert view.headline == f"Scanning {TARGET}…"
    assert "500 files/s" in view.counters


def test_cancelling_freezes_the_bar_where_it_was_and_ignores_late_updates():
    model = _model(ScanEstimate(total_bytes=None, total_files=100, source=ESTIMATE_LAST_SCAN))
    model.handle("walk", _walk(files=40))

    model.request_cancel()
    model.handle("walk", _walk(files=90))  # the scan thread's last snapshot
    assert model.fraction() == 0.4
    assert model.walk.files == 40

    model.finish(CANCELLED)
    assert model.fraction() == 0.4
    assert model.view().percent == "40%"


def test_a_failed_scan_keeps_its_bar_and_a_finished_one_fills_it():
    failed = _model(ScanEstimate(total_bytes=None, total_files=100, source=ESTIMATE_LAST_SCAN))
    failed.handle("walk", _walk(files=30))
    failed.finish(FAILED)
    assert failed.fraction() == 0.3

    finished = _model(None)
    finished.handle("walk", _walk(files=30))
    finished.handle("phase", Phase("Saving history"))
    finished.finish(FINISHED)
    assert finished.fraction() == 1.0
    assert finished.view().percent == "100%"


# -- per-folder bars ---------------------------------------------------------- #


def test_folder_bars_fill_against_last_file_counts_else_the_fullest_folder_and_end_when_done():
    estimate = ScanEstimate(
        total_bytes=None,
        total_files=None,
        source=ESTIMATE_LAST_SCAN,
        folder_files={os.path.normcase("Docs"): 1000},
    )
    model = _model(estimate)
    model.handle(
        "walk",
        _walk(
            top_folders=[
                # A few huge files early on mustn't fill Docs' bar: it's
                # measured in files, like the time the folder takes.
                FolderProgress("Docs", 500, 10**12, SCANNING),
                FolderProgress("New", 400, 5, SCANNING),
                FolderProgress("Media", 9, 800, DONE),
                FolderProgress("Later", 0, 0, QUEUED),
                FolderProgress("Busy", 5000, 1, SCANNING),
            ]
        ),
    )

    fills = {row.name: row.fill for row in model.view().folders}
    assert fills["Docs"] == 0.5  # half its file count last time
    assert fills["New"] == 400 / 5000  # no prior count: against the fullest so far
    assert fills["Busy"] == RUNNING_FRACTION_CAP  # still being read, so never full
    assert fills["Media"] == 1.0
    assert fills["Later"] == 0.0


def test_folder_rows_list_scanning_first_then_queued_then_done_biggest_first():
    model = _model(None)
    model.handle(
        "walk",
        _walk(
            top_folders=[
                FolderProgress("A", 1, 10, DONE),
                FolderProgress("B", 0, 0, QUEUED),
                FolderProgress("C", 1, 30, SCANNING),
                FolderProgress("D", 1, 50, DONE),
                FolderProgress("E", 1, 20, SCANNING),
            ]
        ),
    )

    assert [row.name for row in model.view().folders] == ["C", "E", "B", "D", "A"]


def test_folders_past_the_cap_are_one_last_row_and_count_towards_the_summary():
    model = _model(None)
    model.handle(
        "walk",
        _walk(
            top_folders=[FolderProgress("A", 1, 10, DONE)],
            more_folders=FolderProgress("", 7, 70, DONE, folders=40),
        ),
    )

    view = model.view()
    assert [row.key for row in view.folders][-1] == "more"
    assert "41 of 41" in view.folders_summary
