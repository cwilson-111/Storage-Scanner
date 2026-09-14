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
