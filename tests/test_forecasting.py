import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history as history_db
from storage_scanner.forecasting import forecast_days_until_full
from storage_scanner.ui.history_window import HistoryMixin

GB = 10**9

BASE = datetime(2024, 1, 1)


def _history(points):
    """points: [(day_offset, on-disk size), ...] -> get_forecast_history()
    shape, with the file sizes equal to the on-disk ones."""
    return [((BASE + timedelta(days=day)).isoformat(), size, size) for day, size in points]


def _old_history(points):
    """As saved before on-disk sizes were recorded: file sizes only."""
    return [((BASE + timedelta(days=day)).isoformat(), size, None) for day, size in points]


def test_insufficient_data_below_minimum_points():
    history = _history([(0, 100), (10, 200)])
    forecast = forecast_days_until_full(history, free_bytes=10_000)
    assert forecast.status == "insufficient_data"
    assert forecast.days_estimate is None


def test_not_growing_when_size_is_flat():
    history = _history([(0, 100), (10, 100), (20, 100), (30, 100)])
    forecast = forecast_days_until_full(history, free_bytes=10_000)
    assert forecast.status == "not_growing"


def test_not_growing_when_size_is_shrinking():
    history = _history([(0, 400), (10, 300), (20, 200), (30, 100)])
    forecast = forecast_days_until_full(history, free_bytes=10_000)
    assert forecast.status == "not_growing"


def test_perfect_linear_growth_gives_exact_estimate_and_full_confidence():
    # +10 bytes/day exactly, 11 points over 100 days -> perfect fit; 1,000
    # bytes free lasts 100 days at that rate.
    history = _history([(day, 1000 + day * 10) for day in range(0, 101, 10)])

    forecast = forecast_days_until_full(history, free_bytes=1000)

    assert forecast.status == "ok"
    assert forecast.r_squared > 0.999
    assert forecast.days_estimate == 100
    assert forecast.confidence == "high"
    # A perfect fit means almost no spread between optimistic/pessimistic.
    assert abs(forecast.days_optimistic - forecast.days_pessimistic) < 5


def test_optimistic_bound_gives_more_days_than_pessimistic():
    """'Optimistic' describes days *remaining*, not slope steepness: more
    days before the drive fills is the good-news/optimistic bound, fewer
    days is the bad-news/pessimistic one -- days_optimistic must always be
    the larger of the two whenever noisy history gives them a real spread."""
    points = [(0, 1000), (5, 1300), (10, 1250), (15, 1800), (20, 1700), (25, 2200)]
    history = _history(points)

    forecast = forecast_days_until_full(history, free_bytes=10_000)

    assert forecast.status == "ok"
    assert forecast.days_optimistic is not None
    assert forecast.days_pessimistic is not None
    assert forecast.days_optimistic > forecast.days_pessimistic
    assert forecast.days_pessimistic < forecast.days_estimate < forecast.days_optimistic


def test_a_path_larger_than_its_drive_still_forecasts_from_free_space():
    """This machine's C:\\ history (P1-5): sparse files put its logical total
    at 2.25 TB on a 2.05 TB drive with 318 GB free. That used to read
    "already full"; the room left is the free space, used up at the path's
    growth rate."""
    sizes = [2242.7, 2244.1, 2245.0, 2247.3, 2248.9, 2250.2, 2252.0, 2254.7]
    history = _old_history([(day * 2, int(size * GB)) for day, size in enumerate(sizes)])

    forecast = forecast_days_until_full(history, free_bytes=318 * GB)

    assert forecast.status == "ok"
    assert forecast.on_disk is False
    assert 0.7 * GB < forecast.bytes_per_day < 1.0 * GB
    assert 318 < forecast.days_pessimistic < forecast.days_estimate < 460
    assert forecast.days_estimate < forecast.days_optimistic < 460
    line = HistoryMixin()._format_forecast(forecast)
    assert "already full" not in line
    assert "in file sizes" in line


def test_on_disk_sizes_set_the_rate_once_three_scans_have_them():
    """A sparse file growing 10 GB a day in file size while the path's
    on-disk size grows 1 GB a day: the drive fills at the on-disk rate."""
    history = [
        ((BASE + timedelta(days=day)).isoformat(), (500 + 10 * day) * GB, (100 + day) * GB)
        for day in range(5)
    ]

    forecast = forecast_days_until_full(history, free_bytes=50 * GB)

    assert forecast.on_disk is True
    assert forecast.bytes_per_day == 1 * GB
    assert forecast.days_estimate == 50
    assert "in file sizes" not in HistoryMixin()._format_forecast(forecast)


def test_file_sizes_set_the_rate_until_three_scans_have_on_disk_sizes():
    history = _old_history([(0, 100 * GB), (1, 110 * GB), (2, 120 * GB)])
    history.append(((BASE + timedelta(days=3)).isoformat(), 130 * GB, 90 * GB))
    history.append(((BASE + timedelta(days=4)).isoformat(), 140 * GB, 91 * GB))

    forecast = forecast_days_until_full(history, free_bytes=100 * GB)

    assert forecast.on_disk is False
    assert forecast.data_points == 5
    assert forecast.bytes_per_day == 10 * GB


def test_unknown_free_space_says_so_instead_of_guessing():
    forecast = forecast_days_until_full(_history([(0, 100), (10, 200), (20, 300)]), None)

    assert forecast.status == "free_space_unknown"
    assert forecast.days_estimate is None
    assert "free space" in HistoryMixin()._format_forecast(forecast)


def test_a_drive_with_no_free_space_left_is_full_now():
    forecast = forecast_days_until_full(_history([(0, 100), (10, 200), (20, 300)]), 0)

    assert forecast.status == "ok"
    assert forecast.days_estimate == 0
    assert "no free space left" in HistoryMixin()._format_forecast(forecast)


def test_thin_or_noisy_data_gets_low_confidence():
    # Only 3 points, short span, noisy -> should not claim high confidence.
    history = _history([(0, 100), (2, 400), (4, 250)])
    forecast = forecast_days_until_full(history, free_bytes=100_000)
    assert forecast.confidence == "low"


def _noisy_daily_history():
    """The roadmap's P1-4 case: daily sizes of 100, 80, 120, 85 and 105 GB.
    Growing overall, but so noisy that the slow end of the slope band isn't."""
    return _history([(day, size * GB) for day, size in enumerate([100, 80, 120, 85, 105])])


def test_noisy_growth_has_a_lower_bound_but_no_upper_bound():
    forecast = forecast_days_until_full(_noisy_daily_history(), free_bytes=395 * GB)

    assert forecast.status == "ok"
    assert forecast.days_optimistic is None
    assert 0 < forecast.days_pessimistic <= forecast.days_estimate


def test_growth_history_shows_noisy_growth_as_an_open_ended_range():
    """Growth History's forecast line used to sort [None, int] here and
    raise TypeError, leaving an empty window."""
    forecast = forecast_days_until_full(_noisy_daily_history(), free_bytes=395 * GB)

    line = HistoryMixin()._format_forecast(forecast)

    assert f"at least {forecast.days_pessimistic:,} days" in line


def test_growth_history_shows_a_bounded_range_fewest_days_first():
    history = _history([(0, 1000), (5, 1300), (10, 1250), (15, 1800), (20, 1700), (25, 2200)])
    forecast = forecast_days_until_full(history, free_bytes=10_000)

    line = HistoryMixin()._format_forecast(forecast)

    assert f"{forecast.days_pessimistic:,}–{forecast.days_optimistic:,} days" in line


def test_a_disconnected_drive_forecasts_from_the_free_space_its_last_scan_saw(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history_db, "DB_NAME", str(tmp_path / "storage_history.db"))
    history_db.init_history_db()
    history_db.save_scan_snapshot("q:\\media", 1, 1, 1, 1, {}, drive_free=70 * GB)
    history_db.save_scan_snapshot("q:\\media", 2, 1, 1, 1, {})  # its drive couldn't be read

    free, as_of = HistoryMixin()._free_space_for_forecast(str(tmp_path / "gone"), "q:\\media")

    created_at, _free = history_db.get_latest_drive_free("q:\\media")
    assert free == 70 * GB
    assert as_of == f" at the scan of {created_at.split('T')[0]}"


def test_a_rate_that_would_take_centuries_says_so_instead_of_a_number():
    """P2-5: a forecast from a tiny rate printed "999,000,000,000,000,000 days"."""
    history = _history([(0, 1000 * GB), (1, 1000 * GB + 1), (2, 1000 * GB + 2)])
    forecast = forecast_days_until_full(history, free_bytes=900 * GB)

    assert HistoryMixin()._format_forecast(forecast).count("more than 100 years") == 1
