import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# scale.py imports its sibling modules the way it does when run as a script.
sys.path.insert(0, str(ROOT / "benchmarks"))

_spec = importlib.util.spec_from_file_location("scale", ROOT / "benchmarks" / "scale.py")
scale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scale)

BASELINE = {"tree_bytes_per_file": 500.0, "turbo_small_subtree_records_loaded": 1000}


def test_a_gated_metric_past_the_tolerance_is_a_regression():
    regressions, improvements = scale.compare(
        {"tree_bytes_per_file": 500.0 * 1.2, "turbo_small_subtree_records_loaded": 1000},
        BASELINE,
        tolerance=0.15,
    )

    assert regressions == [("tree_bytes_per_file", 500.0, 600.0)]
    assert improvements == []


def test_noise_within_the_tolerance_passes():
    regressions, improvements = scale.compare(
        {"tree_bytes_per_file": 500.0 * 1.1, "turbo_small_subtree_records_loaded": 950},
        BASELINE,
        tolerance=0.15,
    )

    assert (regressions, improvements) == ([], [])


def test_a_big_improvement_is_reported_so_the_baseline_gets_updated():
    _regressions, improvements = scale.compare(
        {"tree_bytes_per_file": 500.0, "turbo_small_subtree_records_loaded": 51},
        BASELINE,
        tolerance=0.15,
    )

    assert improvements == [("turbo_small_subtree_records_loaded", 1000, 51)]


def test_timings_are_never_gated():
    regressions, _ = scale.compare(
        {"turbo_small_rescan_seconds": 99.0}, {"turbo_small_rescan_seconds": 0.1}
    )

    assert regressions == []


def test_the_synthetic_volume_has_exactly_the_requested_files_parents_first():
    folders = scale.layout(12_345)
    seen = set()

    for parts, _count in folders:
        assert parts == () or parts[:-1] in seen
        seen.add(parts)
    assert sum(count for _parts, count in folders) == 12_345
