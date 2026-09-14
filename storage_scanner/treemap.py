"""Squarified treemap layout — pure geometry, no Tkinter.

Implements the "squarified treemaps" algorithm (Bruls, Huizing, van Wijk):
lay out a list of (item, size) pairs as rectangles that tile a bounding box,
each rectangle's area proportional to its size, favoring near-square shapes
over long thin slivers.
"""


def compute_layout(items, x=0.0, y=0.0, width=100.0, height=100.0):
    """Return [(item, rx, ry, rw, rh), ...] tiling (x, y, width, height).

    `items` is an iterable of (item, size) pairs. Items with size <= 0 are
    dropped (there's no meaningful rectangle to draw for them). Order of
    the input is not preserved — items are laid out largest-first, which
    is what gives the squarified algorithm its near-square rectangles.
    """
    filtered = [(obj, float(size)) for obj, size in items if size and size > 0]
    if not filtered or width <= 0 or height <= 0:
        return []

    filtered.sort(key=lambda pair: pair[1], reverse=True)
    total = sum(size for _, size in filtered)
    if total <= 0:
        return []

    scale = (width * height) / total
    areas = [size * scale for _, size in filtered]

    rects = _squarify(areas, x, y, width, height)
    return [(filtered[i][0],) + rects[i] for i in range(len(filtered))]


def _worst_ratio(row_areas, side):
    """Worst (largest) aspect ratio produced by laying `row_areas` along a
    strip of length `side`. Lower is more square; 1.0 is a perfect square.
    """
    if not row_areas or side <= 0:
        return float("inf")
    row_sum = sum(row_areas)
    row_max = max(row_areas)
    row_min = min(row_areas)
    if row_sum <= 0 or row_min <= 0:
        return float("inf")
    side_sq = side * side
    return max(
        (side_sq * row_max) / (row_sum * row_sum),
        (row_sum * row_sum) / (side_sq * row_min),
    )


def _layout_row(row_areas, row_indices, result, rect):
    """Place one completed row of items, return the remaining rectangle."""
    rx, ry, rw, rh = rect
    total = sum(row_areas)

    if rw >= rh:
        # A vertical column of fixed width, stacked items filling its height.
        col_w = total / rh if rh else 0.0
        cy = ry
        for idx, area in zip(row_indices, row_areas):
            item_h = area / col_w if col_w else 0.0
            result[idx] = (rx, cy, col_w, item_h)
            cy += item_h
        return (rx + col_w, ry, rw - col_w, rh)
    else:
        # A horizontal row of fixed height, items side by side filling width.
        row_h = total / rw if rw else 0.0
        cx = rx
        for idx, area in zip(row_indices, row_areas):
            item_w = area / row_h if row_h else 0.0
            result[idx] = (cx, ry, item_w, row_h)
            cx += item_w
        return (rx, ry + row_h, rw, rh - row_h)


def _squarify(areas, x, y, width, height):
    """Core algorithm: greedily grow a row while it keeps getting more
    square, flush it once adding the next item would make it worse."""
    result = [None] * len(areas)
    remaining = list(range(len(areas)))
    rect = (x, y, width, height)
    row = []

    while remaining:
        _, _, rw, rh = rect
        side = min(rw, rh)
        candidate_idx = remaining[0]
        candidate_row = row + [candidate_idx]

        current_ratio = _worst_ratio([areas[i] for i in row], side) if row else float("inf")
        candidate_ratio = _worst_ratio([areas[i] for i in candidate_row], side)

        if not row or candidate_ratio <= current_ratio:
            row.append(candidate_idx)
            remaining.pop(0)
        else:
            rect = _layout_row([areas[i] for i in row], row, result, rect)
            row = []

    if row:
        _layout_row([areas[i] for i in row], row, result, rect)

    return result
