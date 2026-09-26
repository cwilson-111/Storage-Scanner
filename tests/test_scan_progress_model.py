"""Tests for storage_scanner.scan_progress_model: which estimate a scan's
progress is measured against, and what the progress line shows from the
scan's messages -- the overall bar, the step shown on the tree's root row
while there's no tree yet, and the finished, cancelled and failed states.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner.models import Node
from storage_scanner.scan_history import record_scan
from storage_scanner.scan_progress import Phase, WalkSnapshot
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


def _walk(files=0, nbytes=0, alloc_bytes=0, folders=1):
    return WalkSnapshot(
        files=files,
        bytes=nbytes,
        alloc_bytes=alloc_bytes,
        folders=folders,
        pending=1,
        current_path=None,
        current_seconds=0.0,
        current_entries=0,
    )


def _model(estimate=None, clock=None):
    model = ScanProgressModel(TARGET, clock=clock or _Clock())
    model.handle("estimate", estimate)
    return model


# -- choosing an estimate ------------------------------------------------------ #


def test_the_last_scan_of_the_same_path_wins_over_the_drives_used_space():
    estimate = choose_estimate(LAST_SCAN, volume_used_bytes=10**12)

    assert (estimate.source, estimate.total_files, estimate.disk_bytes) == (
        ESTIMATE_LAST_SCAN,
        400,
        None,
    )


def test_a_volume_root_never_scanned_is_measured_against_its_used_space():
    estimate = choose_estimate(None, volume_used_bytes=123_456)

    assert (estimate.source, estimate.total_files, estimate.disk_bytes) == (
        ESTIMATE_DRIVE_USED,
        None,
        123_456,
    )


def test_an_empty_last_scan_is_no_estimate():
    empty = ("2026-09-24T10:00:00", 0, 0, 1)
    assert choose_estimate(empty, volume_used_bytes=None) is None
    assert choose_estimate(empty, volume_used_bytes=99).source == ESTIMATE_DRIVE_USED
    assert choose_estimate(None, volume_used_bytes=None) is None


def test_the_last_scans_logical_size_is_never_what_the_scan_is_told_to_expect():
    # The real case: C:\ on a 1.9 TB drive with 1.6 TB used, last scanned at
    # 2,254,686,413,442 logical bytes -- 0.5 TB of it one sparse emulator
    # disk image that uses 3.3 GB. "Expecting about ... 2.1 TB" read as a
    # miscount; the scan measures files, so that's what it expects.
    last_c_scan = ("2026-09-25T23:42:29", 2_254_686_413_442, 1_142_488, 260_518)
    model = _model(choose_estimate(last_c_scan, volume_used_bytes=1_733_188_472_832))
    model.handle("walk", _walk(files=10))

    assert model.view().estimate == "Expecting about 1,142,488 files (last scan, 2026-09-25)"


def test_load_estimate_reads_the_last_saved_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    root_path = str(tmp_path / "scanned")
    root = Node(root_path, "scanned")
    big = Node(os.path.join(root_path, "Big"), "Big")
    big.size, big.file_count = 80 * MB, 30
    root.dirs.append(big)
    root.size, root.file_count = big.size, 33
    record_scan(root)

    estimate = load_estimate(root_path)

    assert (estimate.source, estimate.total_files) == (ESTIMATE_LAST_SCAN, 33)
    assert estimate.taken_at


def test_load_estimate_without_history_or_a_volume_root_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()

    assert load_estimate(str(tmp_path)) is None


# -- the overall bar ----------------------------------------------------------- #


def test_the_bar_follows_files_against_the_expected_count_and_never_fills_while_running():
    model = _model(ScanEstimate(total_files=1000, disk_bytes=None, source=ESTIMATE_LAST_SCAN))

    model.handle("walk", _walk(files=250, nbytes=900_000_000))
    assert model.fraction() == 0.25
    assert model.view().percent == "25%"

    model.handle("walk", _walk(files=5000))  # the folder grew since the last scan
    assert model.fraction() == RUNNING_FRACTION_CAP
    assert model.view().percent == "99%"


def test_against_the_drives_used_space_the_bar_follows_bytes_on_disk_not_logical_size():
    model = _model(ScanEstimate(total_files=None, disk_bytes=1000, source=ESTIMATE_DRIVE_USED))

    # A sparse file: huge logical size, little on disk.
    model.handle("walk", _walk(files=10, nbytes=10**12, alloc_bytes=250))

    assert model.fraction() == 0.25
    assert model.view().estimate == "Expecting about 1,000 B on disk (drive's used space)"


def test_without_an_estimate_the_bar_animates_until_a_counted_step_arrives():
    model = _model(None)
    model.handle("walk", _walk(files=10, nbytes=250))
    assert model.fraction() is None
    assert model.view().percent == ""
    assert model.view().estimate == "No earlier scan to compare with"

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
    assert view.headline == "Scanning…"
    assert "500 files/s" in view.counters


def test_a_step_with_no_tree_to_fill_in_is_named_for_the_root_row_until_the_walk_starts():
    model = _model(None)
    model.handle("phase", Phase("Waiting for administrator approval"))
    assert model.view().step == "Waiting for administrator approval…"

    model.handle("phase", Phase("Reading the MFT", 450, 1000, "records"))
    assert model.view().step == "Reading the MFT — 45%"

    model.handle("walk", _walk(files=1))  # Turbo Scan fell back: the tree fills in instead
    assert model.view().step == ""

    model.handle("phase", Phase("Saving history"))
    model.finish(FINISHED)
    assert model.view().step == ""


def test_the_slow_folder_line_names_its_time_and_items_and_keeps_the_end_of_a_long_path():
    model = _model(None)
    deep = os.path.join(TARGET, *(["nested"] * 12), "Manifests")
    model.handle(
        "walk",
        WalkSnapshot(1, 1, 1, 1, 1, current_path=deep, current_seconds=43.2, current_entries=34000),
    )

    current = model.view().current
    assert current.startswith("Now: …") and "Manifests — 43 s, 34,000 items" in current
    assert len(current) < 100


def test_cancelling_freezes_the_bar_where_it_was_and_ignores_late_updates():
    model = _model(ScanEstimate(total_files=100, disk_bytes=None, source=ESTIMATE_LAST_SCAN))
    model.handle("walk", _walk(files=40))

    model.request_cancel()
    model.handle("walk", _walk(files=90))  # the scan thread's last snapshot
    assert model.fraction() == 0.4
    assert model.walk.files == 40

    model.finish(CANCELLED)
    assert model.fraction() == 0.4
    assert model.view().percent == "40%"


def test_a_failed_scan_keeps_its_bar_and_a_finished_one_fills_it():
    failed = _model(ScanEstimate(total_files=100, disk_bytes=None, source=ESTIMATE_LAST_SCAN))
    failed.handle("walk", _walk(files=30))
    failed.finish(FAILED)
    assert failed.fraction() == 0.3

    finished = _model(None)
    finished.handle("walk", _walk(files=30))
    finished.handle("phase", Phase("Saving history"))
    finished.finish(FINISHED)
    assert finished.fraction() == 1.0
    assert finished.view().percent == "100%"
