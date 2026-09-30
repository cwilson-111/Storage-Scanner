"""Confidence-aware forecast of when a drive runs out of free space.

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

The line gives only the path's growth rate. How much room is left comes
from the drive's free space, never from the path's size against the
drive's capacity: sparse files made the logical total of C:\\ larger than
the whole drive (so it read "already full" with 318 GB free), and a
subfolder's size says nothing about everything else on its drive.
"""

from collections import namedtuple
from datetime import datetime

MIN_POINTS_FOR_FORECAST = 3

Forecast = namedtuple(
    "Forecast",
    [
        "status",  # "ok" | "not_growing" | "insufficient_data" | "free_space_unknown"
        "days_estimate",  # point estimate, or None
        "days_optimistic",  # latest plausible run-out (most days remaining), or None
        "days_pessimistic",  # soonest plausible run-out (fewest days remaining), or None
        "confidence",  # "low" | "medium" | "high" | None
        "r_squared",  # fit quality, 0..1, or None
        "data_points",
        "span_days",
        "bytes_per_day",  # the path's fitted growth, or None
        "on_disk",  # True: fitted to on-disk sizes; False: to file sizes; None
        "free_bytes",  # the free space the days count down from, or None
    ],
    defaults=(None, None, None, None, None, None, None, None, None, None),
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


def forecast_days_until_full(history, free_bytes):
    """Forecast how many days the drive's free space lasts if it keeps
    shrinking as fast as this path grows.

    `history` is history.get_forecast_history()'s output: (created_at_iso,
    total_size, allocated_size) rows, oldest first; allocated_size is None
    on scans saved before it was recorded. The growth rate is fitted to the
    on-disk sizes once MIN_POINTS_FOR_FORECAST scans have one, else to the
    file sizes of every scan -- a sparse file's logical size is no guide to
    what it takes on disk, but older history only has file sizes, and it's
    only the rate taken from them. `free_bytes` is the drive's free space
    (None if it can't be told). Returns a Forecast namedtuple; check
    `.status` before trusting any of the numeric fields.
    """
    points = [
        (created_at, allocated) for created_at, _size, allocated in history if allocated is not None
    ]
    on_disk = len(points) >= MIN_POINTS_FOR_FORECAST
    if not on_disk:
        points = [(created_at, size) for created_at, size, _allocated in history]
    if len(points) < MIN_POINTS_FOR_FORECAST:
        return Forecast(status="insufficient_data", data_points=len(points))

    dates = [datetime.fromisoformat(created_at) for created_at, _size in points]
    sizes = [size for _created_at, size in points]
    xs = [(d - dates[0]).total_seconds() / 86400 for d in dates]  # days since first scan
    span_days = xs[-1] - xs[0]

    slope, intercept, r_squared = linear_regression(xs, sizes)

    if slope <= 0:
        return Forecast(
            status="not_growing",
            r_squared=r_squared,
            data_points=len(points),
            span_days=span_days,
            on_disk=on_disk,
        )

    fit = {
        "confidence": _confidence_level(len(points), span_days, r_squared),
        "r_squared": r_squared,
        "data_points": len(points),
        "span_days": round(span_days, 1),
        "bytes_per_day": slope,
        "on_disk": on_disk,
        "free_bytes": free_bytes,
    }
    if free_bytes is None:
        return Forecast(status="free_space_unknown", **fit)
    if free_bytes <= 0:
        return Forecast(status="ok", days_estimate=0, days_optimistic=0, days_pessimistic=0, **fit)

    slope_se = _slope_standard_error(xs, sizes, slope, intercept)
    # A rough +/-1-standard-error band on the slope, translated into a
    # range of run-out dates. "Optimistic"/"pessimistic" describe days
    # *remaining*, not the slope: a shallower (slower-growing) slope
    # runs out later, which is the optimistic (more time left) bound; a
    # steeper (faster-growing) slope runs out sooner, the pessimistic
    # (less time left) bound.
    fast_slope = slope + slope_se
    slow_slope = slope - slope_se
    days_optimistic = free_bytes / slow_slope if slow_slope > 0 else None

    return Forecast(
        status="ok",
        days_estimate=round(free_bytes / slope),
        days_optimistic=round(days_optimistic) if days_optimistic is not None else None,
        days_pessimistic=round(free_bytes / fast_slope),
        **fit,
    )


def format_forecast_range(forecast):
    """The days-until-full range of an "ok" Forecast with days left, as
    text: fewest days first ("~80–120 days"). Noisy history can leave the
    slow end of the slope band not growing at all, and so no upper bound
    (days_optimistic None): that reads "at least N days"."""
    low, high = forecast.days_pessimistic, forecast.days_optimistic
    if high is None:
        return f"at least {low:,} days"
    if low == high:
        return f"~{forecast.days_estimate:,} days"
    return f"~{low:,}–{high:,} days"
