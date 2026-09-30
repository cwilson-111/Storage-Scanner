import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.forecasting import forecast_days_until_full
from storage_scanner.ui.history_window import HistoryMixin

GB = 10**9

BASE = datetime(2024, 1, 1)


def _history(points):
    """points: [(day_offset, total_size), ...] -> get_scan_history() shape."""
    return [((BASE + timedelta(days=day)).isoformat(), size, 0, 0) for day, size in points]


def test_insufficient_data_below_minimum_points():
    history = _history([(0, 100), (10, 200)])
    forecast = forecast_days_until_full(history, drive_capacity_bytes=10_000)
    assert forecast.status == "insufficient_data"
    assert forecast.days_estimate is None


def test_not_growing_when_size_is_flat():
    history = _history([(0, 100), (10, 100), (20, 100), (30, 100)])
    forecast = forecast_days_until_full(history, drive_capacity_bytes=10_000)
    assert forecast.status == "not_growing"


def test_not_growing_when_size_is_shrinking():
    history = _history([(0, 400), (10, 300), (20, 200), (30, 100)])
    forecast = forecast_days_until_full(history, drive_capacity_bytes=10_000)
    assert forecast.status == "not_growing"


def test_perfect_linear_growth_gives_exact_estimate_and_full_confidence():
    # +10 bytes/day exactly, 11 points over 100 days -> perfect fit.
    points = [(day, 1000 + day * 10) for day in range(0, 101, 10)]
    history = _history(points)
    # Last scan is day 100 at size 2000; +10/day means day 200 (100 days
    # after the last scan) reaches exactly 3000.
    capacity = 1000 + 10 * 200

    forecast = forecast_days_until_full(history, drive_capacity_bytes=capacity)

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

    forecast = forecast_days_until_full(history, drive_capacity_bytes=10_000)

    assert forecast.status == "ok"
    assert forecast.days_optimistic is not None
    assert forecast.days_pessimistic is not None
    assert forecast.days_optimistic > forecast.days_pessimistic
    assert forecast.days_pessimistic < forecast.days_estimate < forecast.days_optimistic


def test_already_over_capacity_returns_zero_days():
    history = _history([(0, 100), (10, 500), (20, 900), (30, 1300)])
    forecast = forecast_days_until_full(history, drive_capacity_bytes=1000)
    assert forecast.status == "ok"
    assert forecast.days_estimate == 0


def test_thin_or_noisy_data_gets_low_confidence():
    # Only 3 points, short span, noisy -> should not claim high confidence.
    history = _history([(0, 100), (2, 400), (4, 250)])
    forecast = forecast_days_until_full(history, drive_capacity_bytes=100_000)
    assert forecast.confidence == "low"


def _noisy_daily_history():
    """The roadmap's P1-4 case: daily sizes of 100, 80, 120, 85 and 105 GB.
    Growing overall, but so noisy that the slow end of the slope band isn't."""
    return _history([(day, size * GB) for day, size in enumerate([100, 80, 120, 85, 105])])


def test_noisy_growth_has_a_lower_bound_but_no_upper_bound():
    forecast = forecast_days_until_full(_noisy_daily_history(), drive_capacity_bytes=500 * GB)

    assert forecast.status == "ok"
    assert forecast.days_optimistic is None
    assert 0 < forecast.days_pessimistic <= forecast.days_estimate


def test_growth_history_shows_noisy_growth_as_an_open_ended_range():
    """Growth History's forecast line used to sort [None, int] here and
    raise TypeError, leaving an empty window."""
    forecast = forecast_days_until_full(_noisy_daily_history(), drive_capacity_bytes=500 * GB)

    line = HistoryMixin()._format_forecast(forecast)

    assert f"at least {forecast.days_pessimistic:,} days" in line


def test_growth_history_shows_a_bounded_range_fewest_days_first():
    history = _history([(0, 1000), (5, 1300), (10, 1250), (15, 1800), (20, 1700), (25, 2200)])
    forecast = forecast_days_until_full(history, drive_capacity_bytes=10_000)

    line = HistoryMixin()._format_forecast(forecast)

    assert f"{forecast.days_pessimistic:,}–{forecast.days_optimistic:,} days" in line
