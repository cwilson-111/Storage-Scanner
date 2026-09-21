import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.anomaly_detection import detect_size_anomalies, latest_scan_anomaly

BASE = datetime(2024, 1, 1)


def _history(sizes):
    """sizes: [total_size, ...], one per day starting at BASE."""
    return [
        ((BASE + timedelta(days=i)).isoformat(), size, 0, 0)
        for i, size in enumerate(sizes)
    ]


def test_too_few_deltas_returns_no_anomalies():
    # 4 scans -> 3 deltas, exactly at MIN_DELTAS_FOR_BASELINE, but a
    # baseline of 3 near-identical deltas has ~zero spread, so nothing
    # should be flagged either way.
    history = _history([100, 110, 120, 130])
    assert detect_size_anomalies(history) == []


def test_uniform_growth_has_no_anomalies():
    history = _history([100, 200, 300, 400, 500, 600])
    assert detect_size_anomalies(history) == []


def test_single_spike_among_steady_growth_is_flagged():
    # Steady +100/scan, except one scan that jumped by +5000.
    sizes = [1000, 1100, 1200, 6300, 6400, 6500]
    history = _history(sizes)

    anomalies = detect_size_anomalies(history)

    assert len(anomalies) == 1
    assert anomalies[0].kind == "spike"
    assert anomalies[0].growth_bytes == 5100
    assert anomalies[0].z_score > 2.0


def test_single_drop_among_steady_growth_is_flagged():
    # Steady +100/scan, except one scan that dropped by 5000 (mass deletion shape).
    sizes = [10000, 10100, 10200, 5200, 5300, 5400]
    history = _history(sizes)

    anomalies = detect_size_anomalies(history)

    assert len(anomalies) == 1
    assert anomalies[0].kind == "drop"
    assert anomalies[0].growth_bytes == -5000


def test_unusually_slow_growth_is_a_drop_not_a_spike():
    """A folder that steadily grows ~1000/scan, then grows by only 50 in
    one scan, is still growing (delta > 0) but far slower than its own
    typical pattern -- z is strongly negative, so this must be labeled
    "drop" (relative-to-baseline direction), not "spike" (which the old
    code assigned to any positive delta regardless of z). The literal
    amount must still say "Grew by", never "Shrank by", since delta really
    is positive."""
    sizes = [0, 1000, 2000, 2050, 3050, 4050]
    history = _history(sizes)

    anomalies = detect_size_anomalies(history)

    assert len(anomalies) == 1
    assert anomalies[0].growth_bytes == 50  # still positive: this scan really did grow
    assert anomalies[0].kind == "drop"      # but far slower than usual -> a "drop" vs. baseline
    assert anomalies[0].z_score < 0
    assert "Grew by" in anomalies[0].message
    assert "Shrank" not in anomalies[0].message


def test_unusually_small_shrink_is_a_spike_not_a_drop():
    """Mirror case: a folder that steadily shrinks by ~1000/scan, then
    shrinks by only 50 in one scan (delta < 0, but far less negative than
    usual) has z strongly positive -- must be labeled "spike" (relative-
    to-baseline direction), and the literal amount must still say "Shrank
    by", never "Grew by"."""
    sizes = [8000, 7000, 6000, 5950, 4950, 3950]
    history = _history(sizes)

    anomalies = detect_size_anomalies(history)

    assert len(anomalies) == 1
    assert anomalies[0].growth_bytes == -50  # still negative: this scan really did shrink
    assert anomalies[0].kind == "spike"      # but far less than usual -> a "spike" vs. baseline
    assert anomalies[0].z_score > 0
    assert "Shrank by" in anomalies[0].message
    assert "Grew" not in anomalies[0].message


def test_latest_scan_anomaly_returns_none_when_last_transition_is_normal():
    # The spike is in the middle of history, not the most recent scan.
    sizes = [1000, 1100, 1200, 6300, 6400, 6500]
    history = _history(sizes)
    assert latest_scan_anomaly(history) is None


def test_latest_scan_anomaly_returns_anomaly_when_last_transition_is_the_spike():
    sizes = [1000, 1100, 1200, 1300, 1400, 6500]
    history = _history(sizes)

    anomaly = latest_scan_anomaly(history)

    assert anomaly is not None
    assert anomaly.kind == "spike"
    assert anomaly.created_at == history[-1][0]
