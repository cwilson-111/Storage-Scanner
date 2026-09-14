"""Storage budgets: alert when a folder's tracked size crosses a
user-defined threshold.

Checked at exactly two points: right after a scan of a budgeted path
completes, and once at launch using each budget's last stored scan. This
app has no persistent background service (and isn't meant to grow one),
so there's no continuous monitoring between those points — the `is_stale`
flag on a launch-time breach is what tells the caller how old the number
being compared actually is, so a UI can be honest about it rather than
implying a live reading.
"""

from collections import namedtuple
from datetime import datetime, timezone

from history import get_latest_scan_snapshot, list_budgets

BudgetBreach = namedtuple(
    "BudgetBreach",
    ["path", "threshold_bytes", "current_size_bytes", "as_of", "is_stale"],
)

STALE_AFTER_HOURS = 24


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _is_stale(as_of_iso, now):
    as_of = _parse_iso(as_of_iso)
    if as_of is None:
        return True
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - as_of).total_seconds() > STALE_AFTER_HOURS * 3600


def check_all_budgets(now=None):
    """Every currently-defined budget that's over its threshold, judged
    against each path's last stored scan. A budgeted path with no scan
    history yet is skipped — there's nothing to compare against."""
    now = now or datetime.now(timezone.utc)
    breaches = []
    for _id, path, threshold_bytes, _created_at in list_budgets():
        snapshot = get_latest_scan_snapshot(path)
        if snapshot is None:
            continue
        scanned_at, total_size, _file_count, _folder_count = snapshot
        if total_size > threshold_bytes:
            breaches.append(BudgetBreach(
                path=path, threshold_bytes=threshold_bytes,
                current_size_bytes=total_size, as_of=scanned_at,
                is_stale=_is_stale(scanned_at, now),
            ))
    return breaches


def check_budget_for_path(path, current_size_bytes, now=None):
    """Check one path's budget against a size already on hand (e.g. right
    after a scan completes) — skips the round-trip back through scan
    history. Returns a fresh (never stale) BudgetBreach, or None if there's
    no budget for this path or it isn't exceeded.
    """
    for _id, budget_path, threshold_bytes, _created_at in list_budgets():
        if budget_path == path:
            if current_size_bytes > threshold_bytes:
                now = now or datetime.now(timezone.utc)
                return BudgetBreach(
                    path=path, threshold_bytes=threshold_bytes,
                    current_size_bytes=current_size_bytes,
                    as_of=now.isoformat(), is_stale=False,
                )
            return None
    return None
