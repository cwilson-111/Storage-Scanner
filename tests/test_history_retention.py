"""History retention: which scans storage_scanner.history_retention keeps,
what history.save_scan_snapshot does with the rest, and that forecasting
and anomaly detection read a thinned history the way they read a full one."""

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner import scan_history
from storage_scanner.anomaly_detection import detect_size_anomalies
from storage_scanner.forecasting import forecast_days_until_full
from storage_scanner.history_retention import (
    KEEP_ALL_DAYS_KEY,
    keep_all_days_from_setting,
    scans_to_prune,
)
from storage_scanner.models import Node

NOW = datetime(2026, 6, 15, 12, 0)  # a Monday
FIRST = NOW - timedelta(days=1000)
GB = 1024**3
MB = 1024**2


def _iso(when):
    return when.isoformat(timespec="seconds")


def _pruned(*ages, keep_all_days=30):
    """Which of `ages` (scans that long before NOW) get pruned, with a first
    scan long before them all and a newest scan at NOW around them."""
    times = [FIRST, *(NOW - age for age in ages), NOW]
    scans = [(scan_id, _iso(when)) for scan_id, when in enumerate(times, start=1)]
    pruned = set(scans_to_prune(scans, NOW, keep_all_days))
    return {age for scan_id, age in enumerate(ages, start=2) if scan_id in pruned}


def _days(days, seconds=0):
    return timedelta(days=days, seconds=seconds)


def test_every_scan_inside_the_keep_all_window_is_kept():
    three_a_day = [_days(day, seconds) for day in range(30) for seconds in (60, 600, 3600)]

    assert _pruned(*three_a_day) == set()


def test_thinning_starts_exactly_where_the_keep_all_window_ends():
    earlier_same_day = _days(30, 10)

    assert _pruned(earlier_same_day, _days(30)) == {earlier_same_day}
    assert _pruned(earlier_same_day, _days(30) - timedelta(seconds=1)) == set()


def test_until_90_days_the_newest_scan_of_each_day_is_kept():
    morning, noon, day_after = _days(40, 3 * 3600), _days(40), _days(39)

    assert _pruned(morning, noon, day_after) == {morning}


def test_from_90_days_the_newest_scan_of_each_week_is_kept():
    monday, tuesday = _days(91), _days(90)
    assert (NOW - monday).isocalendar()[:2] == (NOW - tuesday).isocalendar()[:2]

    assert _pruned(monday, tuesday) == {monday}
    # A second younger, Tuesday is still one-a-day: a different day, so both stay.
    assert _pruned(monday, tuesday - timedelta(seconds=1)) == set()


def test_from_a_year_the_newest_scan_of_each_month_is_kept():
    earlier, later = _days(372), _days(365)
    assert (NOW - earlier).month == (NOW - later).month
    assert (NOW - earlier).isocalendar()[1] != (NOW - later).isocalendar()[1]

    assert _pruned(earlier, later) == {earlier}
    assert _pruned(earlier, later - timedelta(seconds=1)) == set()


def test_from_two_years_the_newest_scan_of_each_year_is_kept():
    earlier, later = _days(760), _days(730)
    assert (NOW - earlier).year == (NOW - later).year
    assert (NOW - earlier).month != (NOW - later).month

    assert _pruned(earlier, later) == {earlier}
    assert _pruned(earlier, later - timedelta(seconds=1)) == set()


def test_the_first_scan_is_kept_even_when_a_newer_one_shares_its_bucket():
    first = NOW - _days(100)
    scans = [(1, _iso(first)), (2, _iso(first + timedelta(hours=1))), (3, _iso(NOW))]
    assert scans_to_prune(scans, NOW, 30) == []

    scans.insert(2, (4, _iso(first + timedelta(hours=2))))
    assert scans_to_prune(scans, NOW, 30) == [2]


def test_keeping_forever_prunes_nothing():
    same_day = [(i, _iso(NOW - _days(400, i))) for i in range(1, 6)]

    assert scans_to_prune([*same_day, (9, _iso(NOW))], NOW, None) == []


def test_a_scan_whose_date_cannot_be_read_is_never_pruned():
    scans = [
        (1, _iso(FIRST)),
        (2, "not a date"),
        (3, _iso(NOW - _days(40, 3600))),
        (4, _iso(NOW - _days(40))),
        (5, _iso(NOW)),
    ]

    assert scans_to_prune(scans, NOW, 30) == [3]


@pytest.mark.parametrize(
    "stored, keep_all_days",
    [
        (None, 30),
        ("7", 7),
        (" 365 ", 365),
        ("forever", None),
        ("Forever", None),
        ("0", 30),
        ("-5", 30),
        ("a while", 30),
    ],
)
def test_keep_all_setting_values(stored, keep_all_days):
    assert keep_all_days_from_setting(stored) == keep_all_days


# -- Pruning on save ---------------------------------------------------------- #


class _Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    monkeypatch.setattr(history, "datetime", _Clock)
    history.init_history_db()
    return db_path


def _save_at(when, scan_path, folders):
    _Clock.current = when
    return history.save_scan_snapshot(
        scan_path,
        sum(folders.values()),
        10 * GB,
        len(folders),
        len(folders),
        {path: {"size": size, "file_count": 1} for path, size in folders.items()},
    )


def _scan_ids(scan_path):
    return [row[0] for row in history.list_scans_for_path(scan_path)]


def _query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_saving_prunes_that_paths_thinned_scans_and_their_folder_rows(db):
    day_100 = NOW - _days(100)
    first = _save_at(NOW - _days(200), "C:\\a", {"C:\\a": 10})
    morning = _save_at(day_100 - timedelta(hours=3), "C:\\a", {"C:\\a": 20})
    evening = _save_at(day_100, "C:\\a", {"C:\\a": 30})
    other_morning = _save_at(day_100 - timedelta(hours=3), "C:\\b", {"C:\\b": 1})
    other_evening = _save_at(day_100, "C:\\b", {"C:\\b": 2})

    newest = _save_at(NOW, "C:\\a", {"C:\\a": 40})

    assert _scan_ids("C:\\a") == [newest, evening, first]
    assert _query(db, "SELECT COUNT(*) FROM folder_snapshots WHERE scan_id = ?", (morning,)) == [
        (0,)
    ]
    # Another path's history is only thinned when that path is saved.
    assert _scan_ids("C:\\b") == [other_evening, other_morning]


def test_a_folder_path_is_forgotten_once_no_kept_scan_has_it(db):
    day_100 = NOW - _days(100)
    _save_at(NOW - _days(200), "C:\\a", {"C:\\a": 1, "C:\\a\\in_first": 1})
    _save_at(
        day_100 - timedelta(hours=3),
        "C:\\a",
        {
            "C:\\a": 1,
            "C:\\a\\only_pruned": 1,
            "C:\\a\\in_first": 1,
            "C:\\a\\in_newest": 1,
            "C:\\shared": 1,
        },
    )
    _save_at(day_100, "C:\\a", {"C:\\a": 1})
    _save_at(day_100, "C:\\", {"C:\\": 1, "C:\\shared": 1})

    _save_at(NOW, "C:\\a", {"C:\\a": 1, "C:\\a\\in_newest": 1})

    paths = {path for (path,) in _query(db, "SELECT path FROM folder_paths")}
    assert paths == {"C:\\", "C:\\a", "C:\\a\\in_first", "C:\\a\\in_newest", "C:\\shared"}


@pytest.mark.parametrize(
    "setting, age_days, pruned",
    [
        (None, 10, False),
        ("7", 10, True),
        (None, 40, True),
        ("forever", 40, False),
    ],
)
def test_the_keep_all_setting_decides_how_long_every_scan_is_kept(db, setting, age_days, pruned):
    if setting is not None:
        history.set_app_metadata(KEEP_ALL_DAYS_KEY, setting)
    day = NOW - _days(age_days)
    _save_at(NOW - _days(500), "C:\\a", {"C:\\a": 1})
    earlier_same_day = _save_at(day - timedelta(hours=1), "C:\\a", {"C:\\a": 2})
    _save_at(day, "C:\\a", {"C:\\a": 3})

    _save_at(NOW, "C:\\a", {"C:\\a": 4})

    assert (earlier_same_day not in _scan_ids("C:\\a")) is pruned


def _tree(root_path, big_size):
    root = Node(root_path, "root")
    big = Node(os.path.join(root_path, "big"), "big")
    big.size, big.file_count = big_size, 3
    root.dirs.append(big)
    root.size, root.file_count = big_size, 3
    return root


def test_growth_after_a_pruning_save_is_against_the_scan_just_before_it(db, tmp_path):
    _Clock.current = NOW - _days(200)
    scan_history.record_scan(_tree(str(tmp_path), 60 * MB))
    _Clock.current = NOW - _days(100, 3 * 3600)
    scan_history.record_scan(_tree(str(tmp_path), 70 * MB))
    _Clock.current = NOW - _days(100)
    previous = scan_history.record_scan(_tree(str(tmp_path), 80 * MB))

    _Clock.current = NOW
    latest = scan_history.record_scan(_tree(str(tmp_path), 90 * MB))

    assert latest.previous_scan_id == previous.scan_id
    big = next(row for row in latest.growth_rows if row[0].endswith("big"))
    assert (big[1], big[2]) == (80 * MB, 90 * MB)
    assert len(_scan_ids(scan_history.normalize_scan_path(str(tmp_path)))) == 3


# -- What the readers see after thinning -------------------------------------- #


def _daily_history(days, spike=0):
    """get_scan_history() rows for a scan a day ending at NOW: ~1 GB/day of
    growth with a little deterministic noise, and `spike` extra bytes on
    the last day."""
    rows, size = [], 100 * GB
    for day in range(days):
        size += GB + ((day * 7919) % 101 - 50) * MB
        if day == days - 1:
            size += spike
        rows.append((_iso(NOW - _days(days - 1 - day)), size, 0, 0))
    return rows


def _thinned(rows):
    scans = [(scan_id, row[0]) for scan_id, row in enumerate(rows)]
    pruned = set(scans_to_prune(scans, NOW, 30))
    kept = [row for scan_id, row in enumerate(rows) if scan_id not in pruned]
    assert len(kept) < len(rows) / 2  # the policy really did thin it
    return kept


@pytest.mark.parametrize("spike", [0, 40 * GB])
def test_anomaly_detection_flags_the_same_scans_on_thinned_history(spike):
    full = _daily_history(500, spike)
    flagged = [anomaly.created_at for anomaly in detect_size_anomalies(full)]

    kept_flagged = [anomaly.created_at for anomaly in detect_size_anomalies(_thinned(full))]

    assert kept_flagged == flagged == ([full[-1][0]] if spike else [])


def test_forecast_from_thinned_history_matches_the_full_one():
    full = _daily_history(500)
    capacity = full[-1][1] + 500 * GB

    whole = forecast_days_until_full(full, capacity)
    thinned = forecast_days_until_full(_thinned(full), capacity)

    assert thinned.confidence == whole.confidence == "high"
    assert thinned.span_days == whole.span_days
    assert abs(thinned.days_estimate - whole.days_estimate) <= 0.05 * whole.days_estimate
