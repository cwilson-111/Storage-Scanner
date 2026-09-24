"""Confidence-aware capacity forecasting.

The original forecast (history.estimate_days_until_full) drew a straight
line through only the first and last scan and reported one number — no
sense of whether that line was a good fit, or whether there was even enough
history to trust it. Per the roadmap: "A single straight-line estimate
should never be presented as certainty."

This fits a least-squares line through *every* scan of a path (not just
the endpoints), and reports a range and a confidence level alongside the
point estimate, derived from how much data there is and how well it fits.
Pure Python — no numpy/scipy dependency for what's a small, occasional
calculation.
"""

from collections import namedtuple
from datetime import datetime

MIN_POINTS_FOR_FORECAST = 3

Forecast = namedtuple(
    "Forecast",
    [
        "status",  # "ok" | "not_growing" | "insufficient_data"
        "days_estimate",  # point estimate, or None
        "days_optimistic",  # latest plausible fill date (most days remaining), or None
        "days_pessimistic",  # soonest plausible fill date (fewest days remaining), or None
        "confidence",  # "low" | "medium" | "high" | None
        "r_squared",  # fit quality, 0..1, or None
        "data_points",
        "span_days",
    ],
)


def linear_regression(xs, ys):
    """Least-squares fit of ys = slope*x + intercept, plus R².

    Pure-Python textbook formula — fine at the scale of a few dozen scan
    history points, no numpy needed.
    """
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))

    if ss_xx == 0:
        return 0.0, mean_y, 0.0

    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 1.0

    return slope, intercept, r_squared


def _slope_standard_error(xs, ys, slope, intercept):
    """Standard error of the slope estimate — used to build an optimistic/
    pessimistic range around the point forecast rather than presenting one
    number as certain."""
    n = len(xs)
    if n <= 2:
        return 0.0
    mean_x = sum(xs) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0
    residual_variance = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)) / (n - 2)
    return (residual_variance / ss_xx) ** 0.5


def _confidence_level(data_points, span_days, r_squared):
    """A deliberately conservative, explainable heuristic — not a
    statistical guarantee, just enough to stop a thin data set from
    presenting a forecast as if it were solid."""
    if data_points < 5 or span_days < 14 or r_squared < 0.5:
        return "low"
    if data_points < 10 or span_days < 30 or r_squared < 0.8:
        return "medium"
    return "high"


def forecast_days_until_full(history, drive_capacity_bytes, now=None):
    """Forecast days until `drive_capacity_bytes` is reached, from a scan
    history of a single path.

    `history` is history.get_scan_history()'s output: a list of
    (created_at_iso, total_size, file_count, folder_count) ordered oldest
    first. Returns a Forecast namedtuple; check `.status` before trusting
    any of the numeric fields.
    """
    if len(history) < MIN_POINTS_FOR_FORECAST:
        return Forecast(
            status="insufficient_data",
            days_estimate=None,
            days_optimistic=None,
            days_pessimistic=None,
            confidence=None,
            r_squared=None,
            data_points=len(history),
            span_days=None,
        )

    dates = [datetime.fromisoformat(row[0]) for row in history]
    sizes = [row[1] for row in history]
    first_date = dates[0]
    xs = [(d - first_date).total_seconds() / 86400 for d in dates]  # days since first scan
    span_days = xs[-1] - xs[0]

    slope, intercept, r_squared = linear_regression(xs, sizes)

    if slope <= 0:
        return Forecast(
            status="not_growing",
            days_estimate=None,
            days_optimistic=None,
            days_pessimistic=None,
            confidence=None,
            r_squared=r_squared,
            data_points=len(history),
            span_days=span_days,
        )

    latest_x = xs[-1]
    latest_projected_size = slope * latest_x + intercept
    remaining_bytes = drive_capacity_bytes - latest_projected_size

    if remaining_bytes <= 0:
        return Forecast(
            status="ok",
            days_estimate=0,
            days_optimistic=0,
            days_pessimistic=0,
            confidence=_confidence_level(len(history), span_days, r_squared),
            r_squared=r_squared,
            data_points=len(history),
            span_days=span_days,
        )

    days_estimate = remaining_bytes / slope

    slope_se = _slope_standard_error(xs, sizes, slope, intercept)
    # A rough +/-1-standard-error band on the slope, translated into a
    # range of fill dates. "Optimistic"/"pessimistic" describe days
    # *remaining*, not the slope: a shallower (slower-growing) slope
    # fills later, which is the optimistic (more time left) bound; a
    # steeper (faster-growing) slope fills sooner, the pessimistic
    # (less time left) bound.
    fast_slope = slope + slope_se
    slow_slope = slope - slope_se

    days_pessimistic = remaining_bytes / fast_slope if fast_slope > 0 else days_estimate
    days_optimistic = remaining_bytes / slow_slope if slow_slope > 0 else None

    return Forecast(
        status="ok",
        days_estimate=round(days_estimate),
        days_optimistic=round(days_optimistic) if days_optimistic is not None else None,
        days_pessimistic=round(days_pessimistic) if days_pessimistic is not None else None,
        confidence=_confidence_level(len(history), span_days, r_squared),
        r_squared=r_squared,
        data_points=len(history),
        span_days=round(span_days, 1),
    )
