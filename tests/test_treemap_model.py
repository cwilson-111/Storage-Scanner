"""The treemap pane's model (storage_scanner/treemap_model.py): what gets a
tile, where nested tiles go, which tile a click lands on, and the Growth
colours."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import FileNode, Node
from storage_scanner.scanner import _rollup
from storage_scanner.settings import COLORS
from storage_scanner.treemap_model import (
    GROWTH,
    MAX_TILES,
    MAX_TILES_PER_LEVEL,
    chain,
    hit_test,
    nested_tiles,
    tile_color,
)

W, H = 800, 600


def _folder(parent, name):
    folder = Node(os.path.join(parent.path, name) if parent else os.sep + name, name)
    if parent:
        parent.dirs.append(folder)
    return folder


def _file(folder, name, size, alloc=None):
    return FileNode(folder, folder.add_file(name, size, size if alloc is None else alloc))


def _by_node(tiles):
    return {tile.node: tile for tile in tiles if tile.node is not None}


def test_area_is_on_disk_size_and_files_taking_no_space_get_no_tile():
    root = _folder(None, "root")
    sparse = _file(root, "sparse.vhdx", 10**9, alloc=4096)  # 1 GB logical, 4 KB on disk
    real = _file(root, "real.bin", 400_000)
    empty = _file(root, "empty.txt", 0)
    _rollup(root)

    tiles = _by_node(nested_tiles(root, W, H))

    assert empty not in tiles
    assert tiles[real].w * tiles[real].h > 50 * tiles[sparse].w * tiles[sparse].h


def test_a_subfolders_items_sit_inside_its_tile_below_the_label_strip():
    root = _folder(None, "root")
    sub = _folder(root, "sub")
    inner = [_file(sub, f"f{i}", 1000 * (i + 1)) for i in range(5)]
    _file(root, "top.bin", 10_000)
    _rollup(root)

    tiles = nested_tiles(root, W, H, header=16)
    by_node = _by_node(tiles)
    box = by_node[sub]

    for node in inner:
        tile = by_node[node]
        assert tile.depth == 2 and tiles[tile.parent] is box
        assert box.x <= tile.x and tile.x + tile.w <= box.x + box.w + 1e-6
        assert box.y + 16 <= tile.y and tile.y + tile.h <= box.y + box.h + 1e-6


def test_nesting_stops_at_the_level_limit():
    root = _folder(None, "root")
    level = root
    for depth in range(5):
        level = _folder(level, f"d{depth}")
        _file(level, "f.bin", 1000)
    _rollup(root)

    assert max(tile.depth for tile in nested_tiles(root, W, H, levels=2)) == 2
    assert max(tile.depth for tile in nested_tiles(root, W, H, levels=3)) == 3


def test_a_click_lands_on_the_deepest_tile_and_chain_gives_its_path():
    root = _folder(None, "root")
    sub = _folder(root, "sub")
    deeper = _folder(sub, "deeper")
    target = _file(deeper, "big.bin", 50_000)
    _rollup(root)

    tiles = nested_tiles(root, W, H)
    tile = _by_node(tiles)[target]
    index = hit_test(tiles, tile.x + tile.w / 2, tile.y + tile.h / 2)

    assert tiles[index].node == target
    assert chain(tiles, index) == [sub, deeper, target]
    assert hit_test(tiles, -1, -1) is None


def test_items_past_the_per_level_limit_share_one_more_tile():
    root = _folder(None, "root")
    extra = 50
    for i in range(MAX_TILES_PER_LEVEL + extra):
        _file(root, f"f{i:04}", 10_000 + i)  # f0000 is the smallest
    _rollup(root)

    tiles = nested_tiles(root, 4000, 4000, min_px=1)
    more = [tile for tile in tiles if tile.node is None]

    assert len(more) == 1
    assert more[0].rest_count == extra
    assert more[0].rest_bytes == sum(10_000 + i for i in range(extra))
    assert len(tiles) == MAX_TILES_PER_LEVEL + 1


def test_the_whole_drawing_is_capped_without_leaving_top_level_items_blank():
    root = _folder(None, "root")
    subs = [_folder(root, f"d{d}") for d in range(40)]
    for sub in subs:
        for f in range(150):
            _file(sub, f"f{f}", 1000)
    _rollup(root)

    tiles = nested_tiles(root, 4000, 4000, min_px=1, header=2)
    by_node = _by_node(tiles)

    assert len(tiles) <= MAX_TILES
    # 40 + 40 x 150 tiles won't fit: every folder still gets its own tile,
    # and each is either filled with all of its files or left whole.
    assert all(sub in by_node for sub in subs)
    parents = [tiles[t.parent].node for t in tiles if t.parent is not None]
    inside = [sum(node is sub for node in parents) for sub in subs]
    assert set(inside) == {0, 150}
    assert inside.count(150) == (MAX_TILES - len(subs)) // 150


def test_growth_colours_a_folder_by_its_change_and_a_file_by_its_folders():
    root = _folder(None, "root")
    grew, shrank, unknown = _folder(root, "grew"), _folder(root, "shrank"), _folder(root, "new")
    in_grew = _file(grew, "a.bin", 2000)
    _file(shrank, "b.bin", 1000)
    _file(unknown, "c.bin", 1000)
    _rollup(root)
    changes = {grew: +1000, shrank: -1000}
    tiles = _by_node(nested_tiles(root, W, H))

    def colour(node):
        return tile_color(tiles[node], GROWTH, changes.get)

    assert colour(unknown) == COLORS["heat_low"]
    assert colour(in_grew) == colour(grew)
    assert colour(grew) != colour(shrank)
    assert colour(grew) != COLORS["heat_low"] and colour(shrank) != COLORS["heat_low"]
    # No previous scan at all: everything is the quiet colour.
    assert tile_color(tiles[grew], GROWTH, None) == COLORS["heat_low"]
