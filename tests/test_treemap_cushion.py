"""The treemap's cushion shading (storage_scanner/treemap_cushion.py): where
the light falls, that nested tiles take their folder's bulge, that the
fast table-driven image is the cushion model pixel for pixel, and that a
label's colour comes from the pixel under it."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import FileNode, Node
from storage_scanner.scanner import _rollup
from storage_scanner.treemap_cushion import AMBIENT, HEIGHT, SCALE, light, render, shade
from storage_scanner.treemap_model import Tile, hit_test, nested_tiles

FILL = "#c08040"
BACKGROUND = "#102030"


def _tile(x, y, w, h, depth=1, parent=None):
    return Tile(None, x, y, w, h, depth, 0.0, parent)


def _pixels(tiles, fills, width, height):
    """pixel(x, y) -> (r, g, b) of the rendered image."""
    data = render(tiles, fills, width, height, BACKGROUND)
    header = b"P6\n%d %d\n255\n" % (width, height)
    assert data.startswith(header) and len(data) == len(header) + 3 * width * height
    body = data[len(header) :]

    def pixel(x, y):
        start = 3 * (y * width + x)
        return tuple(body[start : start + 3])

    return pixel


def _hex(rgb):
    return "#" + bytes(rgb).hex()


def test_a_tile_is_its_fill_on_top_lit_from_the_top_left_and_dark_at_the_edges():
    pixel = _pixels([_tile(0, 0, 101, 101)], [FILL], 102, 101)

    top = pixel(50, 50)  # the ridge's top: flat
    assert _hex(top) == FILL
    assert sum(pixel(10, 50)) > sum(pixel(90, 50))  # left of the top vs as far right
    assert sum(pixel(50, 10)) > sum(pixel(50, 90))  # above vs below
    for corner in ((0, 0), (100, 0), (0, 100), (100, 100)):
        assert all(c < t for c, t in zip(pixel(*corner), top))
    assert _hex(pixel(101, 50)) == BACKGROUND  # outside every tile


def test_items_take_their_folders_bulge():
    # Two identical files, one each side of their folder's ridge.
    folder = _tile(0, 0, 300, 100)
    left = _tile(20, 30, 60, 60, depth=2, parent=0)
    right = _tile(220, 30, 60, 60, depth=2, parent=0)
    pixel = _pixels([folder, left, right], ["#808080"] * 3, 300, 100)

    # The one on the side facing the light is the brighter at its middle.
    assert sum(pixel(50, 60)) > sum(pixel(250, 60))


def _expected(tiles, index, fill, x, y):
    """The cushion model at pixel (x, y) of tile `index`, worked out per
    pixel: the tile's slopes are the sums of every ridge it's on."""
    slope_x = slope_y = 0.0
    while index is not None:
        tile = tiles[index]
        height = 4 * HEIGHT * SCALE ** (tile.depth - 1)
        slope_x += height * (2 * tile.x + tile.w - 2 * (x + 0.5)) / tile.w
        slope_y += height * (2 * tile.y + tile.h - 2 * (y + 0.5)) / tile.h
        index = tile.parent
    cosine = max(0.0, min(1.0, light(slope_x, slope_y)))
    brightness = (AMBIENT + (1 - AMBIENT) * cosine) / (AMBIENT + (1 - AMBIENT) * light(0, 0))
    return [min(255, value * brightness) for value in bytes.fromhex(fill[1:])]


def test_every_pixel_is_the_cushion_model_however_deep_the_nesting():
    # Ten levels, each tucked into its parent's bottom right corner, where
    # every ridge slopes away from the light at once: the steepest slopes
    # nesting can make, and the corners a separable shading gets wrong.
    tiles = [_tile(0, 0, 160, 120)]
    for depth in range(2, 11):
        parent = tiles[-1]
        tiles.append(
            _tile(parent.x + 12, parent.y + 9, parent.w - 12, parent.h - 9, depth, depth - 2)
        )
    pixel = _pixels(tiles, [FILL] * len(tiles), 160, 120)

    worst = 0.0
    for y in range(120):
        for x in range(160):
            index = hit_test(tiles, x + 0.5, y + 0.5)
            expected = _expected(tiles, index, FILL, x, y)
            worst = max(worst, *(abs(a - b) for a, b in zip(pixel(x, y), expected)))
    assert worst <= 6  # of 255: binning slopes and light into bytes


def test_a_labels_colour_is_the_pixel_under_it():
    root = Node(os.sep + "root", "root")
    for d in range(4):
        sub = Node(os.path.join(root.path, f"d{d}"), f"d{d}")
        root.dirs.append(sub)
        for f in range(6):
            FileNode(sub, sub.add_file(f"f{f}.bin", 1000 * (f + d + 1), 1000 * (f + d + 1)))
    _rollup(root)
    tiles = nested_tiles(root, 400, 300, header=16)
    fills = [f"#{40 * i % 256:02x}5a{(200 - 7 * i) % 200:02x}" for i in range(len(tiles))]
    pixel = _pixels(tiles, fills, 400, 300)

    for index, tile in enumerate(tiles):
        x, y = int(tile.x + tile.w / 2), int(tile.y + tile.h / 2)
        if hit_test(tiles, x + 0.5, y + 0.5) == index:
            assert shade(tiles, index, fills[index], x + 0.3, y + 0.7) == _hex(pixel(x, y))
