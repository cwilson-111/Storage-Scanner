"""Tests for HistoryMixin._likely_folder_for_anomaly: best-effort
correlation of a scan-level size anomaly (which only knows the root path's
total changed) back to the specific tracked folder most likely responsible,
via history.get_folder_growth for the same pair of scans the anomaly itself
compares.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.anomaly_detection import Anomaly
from storage_scanner.ui import history_window
from storage_scanner.ui.history_window import HistoryMixin

SCAN_PATH = "C:/Example"


def _row(folder_path, growth_bytes, previous_size=1000, current_size=None):
    current_size = previous_size + growth_bytes if current_size is None else current_size
    growth_percent = (growth_bytes / previous_size) * 100 if previous_size else None
    growth_type = "Growing" if growth_bytes > 0 else "Shrinking" if growth_bytes < 0 else "Unchanged"
    return (folder_path, previous_size, current_size, growth_bytes, growth_percent, growth_type, 10)


def _drop_anomaly(created_at="2024-02-01T00:00:00"):
    return Anomaly(created_at=created_at, kind="drop", growth_bytes=-5000, z_score=-4.0, message="dropped")


def _spike_anomaly(created_at="2024-02-01T00:00:00"):
    return Anomaly(created_at=created_at, kind="spike", growth_bytes=5000, z_score=4.0, message="spiked")


def _setup(monkeypatch, rows):
    monkeypatch.setattr(history_window, "get_folder_growth", lambda current, previous, limit=50: rows)
    return HistoryMixin()


def _order_and_ids():
    created_ats = ["2024-01-01T00:00:00", "2024-02-01T00:00:00"]
    scan_ids = {"2024-01-01T00:00:00": 1, "2024-02-01T00:00:00": 2}
    return created_ats, scan_ids


def test_drop_anomaly_picks_the_folder_that_shrank_the_most(monkeypatch):
    rows = [
        _row(SCAN_PATH, growth_bytes=-9000),           # root itself -- must be excluded
        _row("C:/Example/Downloads", growth_bytes=-8000),
        _row("C:/Example/Photos", growth_bytes=-500),
    ]
    app = _setup(monkeypatch, rows)
    created_ats, scan_ids = _order_and_ids()

    folder = app._likely_folder_for_anomaly(SCAN_PATH, _drop_anomaly(), created_ats, scan_ids)

    assert folder == "C:/Example/Downloads"


def test_spike_anomaly_picks_the_folder_that_grew_the_most(monkeypatch):
    rows = [
        _row(SCAN_PATH, growth_bytes=9000),             # root itself -- must be excluded
        _row("C:/Example/Downloads", growth_bytes=8000),
        _row("C:/Example/Photos", growth_bytes=500),
    ]
    app = _setup(monkeypatch, rows)
    created_ats, scan_ids = _order_and_ids()

    folder = app._likely_folder_for_anomaly(SCAN_PATH, _spike_anomaly(), created_ats, scan_ids)

    assert folder == "C:/Example/Downloads"


def test_returns_none_when_only_the_root_folder_is_tracked(monkeypatch):
    rows = [_row(SCAN_PATH, growth_bytes=-9000)]
    app = _setup(monkeypatch, rows)
    created_ats, scan_ids = _order_and_ids()

    assert app._likely_folder_for_anomaly(SCAN_PATH, _drop_anomaly(), created_ats, scan_ids) is None


def test_returns_none_when_no_tracked_folder_moved_in_the_anomalys_direction(monkeypatch):
    # A drop anomaly, but every tracked non-root folder actually grew --
    # nothing here honestly explains the drop, so don't guess.
    rows = [_row("C:/Example/Downloads", growth_bytes=200), _row("C:/Example/Photos", growth_bytes=50)]
    app = _setup(monkeypatch, rows)
    created_ats, scan_ids = _order_and_ids()

    assert app._likely_folder_for_anomaly(SCAN_PATH, _drop_anomaly(), created_ats, scan_ids) is None


def test_returns_none_when_the_anomalys_created_at_is_not_in_the_history(monkeypatch):
    app = _setup(monkeypatch, [_row("C:/Example/Downloads", growth_bytes=-8000)])
    created_ats, scan_ids = _order_and_ids()

    anomaly = _drop_anomaly(created_at="1999-01-01T00:00:00")
    assert app._likely_folder_for_anomaly(SCAN_PATH, anomaly, created_ats, scan_ids) is None


def test_returns_none_for_the_very_first_scan_with_no_previous_scan(monkeypatch):
    app = _setup(monkeypatch, [_row("C:/Example/Downloads", growth_bytes=-8000)])
    created_ats, scan_ids = _order_and_ids()

    anomaly = _drop_anomaly(created_at=created_ats[0])  # index 0 -- nothing before it
    assert app._likely_folder_for_anomaly(SCAN_PATH, anomaly, created_ats, scan_ids) is None


def test_returns_none_when_a_scan_id_is_missing_from_the_mapping(monkeypatch):
    app = _setup(monkeypatch, [_row("C:/Example/Downloads", growth_bytes=-8000)])
    created_ats = ["2024-01-01T00:00:00", "2024-02-01T00:00:00"]
    scan_ids = {"2024-02-01T00:00:00": 2}  # missing the previous scan's id

    assert app._likely_folder_for_anomaly(SCAN_PATH, _drop_anomaly(), created_ats, scan_ids) is None
