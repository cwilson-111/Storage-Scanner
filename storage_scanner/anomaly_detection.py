"""Size-based anomaly detection over a path's scan history.

Flags a scan-to-scan change in total size that's a statistical outlier
relative to that path's own typical growth/shrink pattern — a sudden spike
(much faster growth than usual) or a sudden drop (much larger shrink than
usual, shaped like a mass deletion). This works on data already stored by
every scan (history.get_scan_history); no new tables needed.

This is a lead worth checking, the same way Review-candidate recommendations
are — not a verified diagnosis. A z-score outlier is exactly that: unusual
*for this folder*, not proof of anything in particular.
"""

import statistics
from collections import namedtuple

from storage_scanner.formatting import human_size

MIN_DELTAS_FOR_BASELINE = 3
DEFAULT_Z_THRESHOLD = 2.0

Anomaly = namedtuple(
    "Anomaly", ["created_at", "kind", "growth_bytes", "z_score", "message"],
)


def _deltas(history):
    """[(created_at, delta_bytes), ...] between consecutive scans."""
    return [
        (history[i][0], history[i][1] - history[i - 1][1])
        for i in range(1, len(history))
    ]


def detect_size_anomalies(history, z_threshold=DEFAULT_Z_THRESHOLD):
    """Flag scan-to-scan size changes that are statistical outliers for
    this path's own history.

    `history` is history.get_scan_history()'s output, oldest first.
    Returns a list of Anomaly, oldest first. Needs at least
    MIN_DELTAS_FOR_BASELINE+1 scans to have any baseline to compare
    against; returns [] otherwise (not "no anomalies found" so much as
    "not enough history to judge yet").

    Each point is scored against a baseline built from *every other*
    delta (leave-one-out), not the full population including itself — a
    single big outlier otherwise inflates its own comparison's standard
    deviation enough to mask itself (a well-known effect: with 5 fairly
    steady deltas plus one huge spike, the spike's own z-score gets pulled
    below threshold because it dominates the variance it's being measured
    against).
    """
    deltas = _deltas(history)
    if len(deltas) < MIN_DELTAS_FOR_BASELINE:
        return []

    values = [d for _created_at, d in deltas]

    anomalies = []
    for index, (created_at, delta) in enumerate(deltas):
        others = values[:index] + values[index + 1:]
        baseline_mean = statistics.mean(others)
        try:
            baseline_stdev = statistics.stdev(others)
        except statistics.StatisticsError:
            baseline_stdev = 0.0

        if baseline_stdev == 0:
            # A perfectly steady baseline with no variance to divide by —
            # any deviation at all from it is the anomaly signal here.
            if delta == baseline_mean:
                continue
            z = float("inf") if delta > baseline_mean else float("-inf")
        else:
            z = (delta - baseline_mean) / baseline_stdev
            if abs(z) < z_threshold:
                continue

        score_text = "far outside its usual pattern" if z in (float("inf"), float("-inf")) else f"z-score {z:+.1f}"
        if delta > 0:
            kind = "spike"
            message = (
                f"Grew by {human_size(delta)} in one scan — much faster than "
                f"this folder's typical growth ({score_text})."
            )
        else:
            kind = "drop"
            message = (
                f"Shrank by {human_size(abs(delta))} in one scan — far more "
                f"than usual, worth checking it was intentional ({score_text})."
            )
        anomalies.append(Anomaly(
            created_at=created_at, kind=kind, growth_bytes=delta,
            z_score=z, message=message,
        ))

    return anomalies


def latest_scan_anomaly(history, z_threshold=DEFAULT_Z_THRESHOLD):
    """The Anomaly for the most recent scan-to-scan transition, if that
    specific transition is itself an outlier — else None. Meant for a
    right-after-scan banner, distinct from detect_size_anomalies()'s full
    history list."""
    if len(history) < 2:
        return None
    latest_created_at = history[-1][0]
    for anomaly in detect_size_anomalies(history, z_threshold):
        if anomaly.created_at == latest_created_at:
            return anomaly
    return None
