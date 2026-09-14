import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.treemap import compute_layout


def _total_area(rects):
    return sum(rw * rh for _, _, _, rw, rh in rects)


def test_empty_input_returns_empty():
    assert compute_layout([], 0, 0, 100, 100) == []


def test_zero_and_negative_sizes_are_dropped():
    result = compute_layout([("a", 0), ("b", -5), ("c", 10)], 0, 0, 100, 100)
    assert [item for item, *_ in result] == ["c"]


def test_zero_width_or_height_returns_empty():
    assert compute_layout([("a", 10)], 0, 0, 0, 100) == []
    assert compute_layout([("a", 10)], 0, 0, 100, 0) == []


def test_single_item_fills_entire_box():
    result = compute_layout([("only", 42)], 10, 20, 200, 100)
    assert len(result) == 1
    item, rx, ry, rw, rh = result[0]
    assert item == "only"
    assert (rx, ry, rw, rh) == (10, 20, 200, 100)


def test_total_area_matches_bounding_box():
    items = [("a", 500), ("b", 300), ("c", 150), ("d", 50)]
    result = compute_layout(items, 0, 0, 400, 300)
    assert abs(_total_area(result) - 400 * 300) < 1e-6


def test_areas_are_proportional_to_input_sizes():
    items = [("big", 800), ("small", 200)]
    result = compute_layout(items, 0, 0, 500, 200)
    areas = {item: rw * rh for item, _, _, rw, rh in result}
    # "big" is 4x the size of "small", so its rectangle should be ~4x the area.
    ratio = areas["big"] / areas["small"]
    assert abs(ratio - 4.0) < 0.05


def test_equal_sizes_get_equal_areas():
    items = [(f"item{i}", 100) for i in range(5)]
    result = compute_layout(items, 0, 0, 300, 200)
    areas = [rw * rh for _, _, _, rw, rh in result]
    expected = (300 * 200) / 5
    for area in areas:
        assert abs(area - expected) < 1.0


def test_rectangles_stay_within_bounding_box():
    items = [("a", 733), ("b", 291), ("c", 88), ("d", 12), ("e", 401)]
    result = compute_layout(items, 5, 5, 250, 180)
    for _, rx, ry, rw, rh in result:
        assert rx >= 5 - 1e-6
        assert ry >= 5 - 1e-6
        assert rx + rw <= 5 + 250 + 1e-6
        assert ry + rh <= 5 + 180 + 1e-6
        assert rw > 0
        assert rh > 0


def test_all_input_items_are_represented_when_positive():
    items = [("a", 10), ("b", 20), ("c", 30)]
    result = compute_layout(items, 0, 0, 100, 100)
    assert {item for item, *_ in result} == {"a", "b", "c"}
    assert len(result) == 3


def test_many_items_of_varied_size_tile_without_overlap_area_check():
    # Overlap is hard to check directly without a full geometry sweep, but
    # if rectangles overlapped or left gaps, total area would drift from
    # the bounding box — this is the practical invariant that matters.
    items = [(i, (i % 13) + 1) for i in range(50)]
    result = compute_layout(items, 0, 0, 640, 480)
    assert len(result) == 50
    assert abs(_total_area(result) - 640 * 480) < 1e-3
