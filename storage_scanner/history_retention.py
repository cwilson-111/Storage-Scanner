"""Which saved scans scan history keeps: every recent scan, then fewer and
fewer older ones, so the history database stops growing with every scan.

For each scan path, relative to the scan just saved:

- every scan younger than the keep-all window (30 days by default,
  app_metadata key "history_keep_all_days") is kept;
- older scans keep one per calendar day until 90 days old, then one per
  ISO week until a year old, then one per month until two years old, then
  one per year. In each of those buckets the newest scan survives, so the
  scan a new save is compared against (the one just before it) is never
  the one pruned;
- the first scan of a path is always kept too, however old.

A daily scheduled scan therefore settles at about 145 scans per path, plus
one a year after the second year, instead of growing without end.

What the readers need, and why this policy gives it to them:

- Forecasting (storage_scanner.forecasting) fits a line through every
  scan's (date, size). Thinning old points leaves the line where it was;
  keeping the first scan keeps the full time span its confidence level
  depends on.
- Anomaly detection (storage_scanner.anomaly_detection) scores each
  scan-to-scan change against the path's other changes. Thinned scans are
  days, weeks or months apart, so a gap of k days is scored as k days of
  growth: expected to be k times the usual daily rate, with k times a
  day's variance (see its _z_score). A month of ordinary growth is then no
  spike, and a week's averaged-out growth doesn't make one day's ordinary
  noise look like one either; evenly spaced scans score as they always did.
- Growth details and the snapshot picker work on any two scans still kept.
"""

from collections.abc import Hashable, Sequence
from datetime import datetime, timedelta
from typing import Optional

KEEP_ALL_DAYS_KEY = "history_keep_all_days"
DEFAULT_KEEP_ALL_DAYS = 30
# The setting's value for "never thin history".
KEEP_FOREVER = "forever"

# (minimum age, bucket), coarsest first: a scan at least this old keeps
# only the newest scan in its bucket.
_TIERS = (
    (timedelta(days=730), "year"),
    (timedelta(days=365), "month"),
    (timedelta(days=90), "week"),
    (timedelta(days=0), "day"),
)


def keep_all_days_from_setting(value: Optional[str]) -> Optional[int]:
    """The keep-all window in days for a stored setting value, or None for
    KEEP_FOREVER (never prune). Missing or unreadable values get the
    default rather than failing a save."""
    if value is None:
        return DEFAULT_KEEP_ALL_DAYS
    value = value.strip().lower()
    if value == KEEP_FOREVER:
        return None
    try:
        days = int(value)
    except ValueError:
        return DEFAULT_KEEP_ALL_DAYS
    return days if days >= 1 else DEFAULT_KEEP_ALL_DAYS


def _bucket(created: datetime, age: timedelta) -> Hashable:
    unit = next(unit for min_age, unit in _TIERS if age >= min_age)
    if unit == "year":
        return (unit, created.year)
    if unit == "month":
        return (unit, created.year, created.month)
    if unit == "week":
        iso_year, iso_week, _weekday = created.isocalendar()
        return (unit, iso_year, iso_week)
    return (unit, created.date())


def scans_to_prune(
    scans: Sequence[tuple[int, str]], now: datetime, keep_all_days: Optional[int]
) -> list[int]:
    """Ids of the scans retention drops, from every (id, created_at) scan
    of one scan path. `now` is when the newest scan was saved. A scan whose
    created_at can't be parsed is never dropped."""
    if keep_all_days is None:
        return []

    dated = []
    for scan_id, created_at in scans:
        try:
            dated.append((datetime.fromisoformat(created_at), scan_id))
        except (TypeError, ValueError):
            continue
    if not dated:
        return []
    dated.sort()
    first_id = dated[0][1]

    keep_all = timedelta(days=keep_all_days)
    newest_in_bucket: dict[Hashable, int] = {}
    bucketed = []
    for created, scan_id in dated:  # oldest first: the last one seen wins
        age = now - created
        if age < keep_all:
            continue
        bucket = _bucket(created, age)
        newest_in_bucket[bucket] = scan_id
        bucketed.append((bucket, scan_id))

    return [
        scan_id
        for bucket, scan_id in bucketed
        if newest_in_bucket[bucket] != scan_id and scan_id != first_id
    ]
