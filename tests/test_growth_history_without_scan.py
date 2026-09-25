"""Growth History has to work straight after launch, from scans saved by
earlier runs (and earlier versions) -- not only after a scan this session."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner.scan_history import normalize_scan_path
from storage_scanner.ui.history_window import HistoryMixin


class _PathBox:
    def __init__(self, text):
        self.text = text

    def get(self):
        return self.text


class _App(HistoryMixin):
    def __init__(self, typed):
        self.path_var = _PathBox(typed)


def _db_with_scans(tmp_path, monkeypatch, *paths):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()
    for path in paths:
        history.save_scan_snapshot(normalize_scan_path(path), 1, 1, 1, 1, {})


def test_most_recent_scan_path_is_the_newest_scan_of_anything(tmp_path, monkeypatch):
    _db_with_scans(tmp_path, monkeypatch, r"C:\Data", r"D:\Media", r"C:\Data")

    assert history.get_most_recent_scan_path() == normalize_scan_path(r"C:\Data")


def test_most_recent_scan_path_is_none_before_any_scan(tmp_path, monkeypatch):
    _db_with_scans(tmp_path, monkeypatch)

    assert history.get_most_recent_scan_path() is None


def test_the_path_in_the_path_box_wins_when_it_has_history(tmp_path, monkeypatch):
    _db_with_scans(tmp_path, monkeypatch, r"C:\Data", r"D:\Media")

    # Typed differently from how it was stored (case, trailing separator).
    assert _App("c:\\data\\")._history_path_without_scan() == normalize_scan_path(r"C:\Data")


def test_a_path_box_without_history_falls_back_to_the_latest_scan(tmp_path, monkeypatch):
    _db_with_scans(tmp_path, monkeypatch, r"C:\Data", r"D:\Media")

    for typed in (r"E:\Never scanned", "", '"   "'):
        assert _App(typed)._history_path_without_scan() == normalize_scan_path(r"D:\Media")
