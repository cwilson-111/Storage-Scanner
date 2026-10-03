"""What the treemap pane draws: a folder as nested tiles two or three
levels deep, each tile's area its on-disk size, and the colour each mode
gives a tile. No Tk here; ui/treemap_pane.py puts it on a canvas.

A level shows at most MAX_TILES_PER_LEVEL items, largest first, and the
rest share one "N more" tile, so a folder of 250,000 files draws no more
tiles than one of 200 (picking them from its size column takes about
0.1 s). Tiles smaller than MIN_TILE_PX on a side aren't drawn
(their parent's colour shows through), and a folder only gets tiles inside
it when there's room below its label strip.
"""

import heapq
import os
import time
import zlib
from dataclasses import dataclass
from typing import Any, Optional

from storage_scanner.models import FileNode
from storage_scanner.settings import COLORS, heat_color
from storage_scanner.treemap import compute_layout

MAX_TILES_PER_LEVEL = 200
MAX_TILES = 3000  # the whole drawing; Tk canvases slow down well past this
MIN_TILE_PX = 4
HEADER_PX = 16  # a folder tile's label strip, above its own tiles

SIZE, TYPE, AGE, GROWTH = "size", "type", "age", "growth"
MODES = ((SIZE, "Size"), (TYPE, "File type"), (AGE, "Last modified"), (GROWTH, "Growth"))


@dataclass(frozen=True)
class Tile:
    node: Any  # a Node or FileNode; None for an "N more" tile
    x: float
    y: float
    w: float
    h: float
    depth: int  # 1 for the shown folder's own items
    share: float  # area against the largest tile beside it (for SIZE)
    parent: Optional[int]  # index of the enclosing folder tile, if any
    rest_count: int = 0  # how many items an "N more" tile stands for
    rest_bytes: int = 0


def area_of(node):
    """A tile's area: the bytes it really takes on disk."""
    return node.alloc_size


def nested_tiles(folder, width, height, levels=3, header=HEADER_PX, min_px=MIN_TILE_PX):
    """Tiles for `folder` in a width x height box: its items, and inside
    each subfolder tile its items, `levels` deep. Parents come before
    their children, so the last tile containing a point is the deepest."""
    tiles = []  # type: ignore[var-annotated]
    _fill(tiles, folder, 0.0, 0.0, float(width), float(height), 1, None, levels, header, min_px)
    return tiles


def _largest_items(folder):
    """The folder's MAX_TILES_PER_LEVEL largest items that take space,
    largest first, and (count, bytes) of its other items that do. Files are
    picked from the folder's size column, so only the files shown become
    FileNode views, however many the folder holds."""
    allocs = folder.file_allocs
    rows = [i for i in folder.file_rows() if allocs[i] > 0]
    top_rows = heapq.nlargest(MAX_TILES_PER_LEVEL, rows, key=allocs.__getitem__)
    dirs = [d for d in folder.dirs if d.alloc_size > 0]
    candidates = dirs + [FileNode(folder, i) for i in top_rows]
    shown = sorted(candidates, key=area_of, reverse=True)[:MAX_TILES_PER_LEVEL]
    total_bytes = sum(d.alloc_size for d in dirs) + sum(allocs[i] for i in rows)
    rest_count = len(dirs) + len(rows) - len(shown)
    return shown, rest_count, total_bytes - sum(area_of(item) for item in shown)


def _fill(tiles, folder, x, y, w, h, depth, parent, levels, header, min_px):
    shown, rest_count, rest_bytes = _largest_items(folder)
    pairs = [(child, area_of(child)) for child in shown]
    if rest_count:
        pairs.append(((rest_count, rest_bytes), rest_bytes))
    largest = area_of(shown[0]) if shown else 1
    for item, rx, ry, rw, rh in compute_layout(pairs, x, y, w, h):
        if rw < min_px or rh < min_px or len(tiles) >= MAX_TILES:
            continue
        if isinstance(item, tuple):  # the "N more" tile
            count, size = item
            tiles.append(Tile(None, rx, ry, rw, rh, depth, 0.0, parent, count, size))
            continue
        tiles.append(Tile(item, rx, ry, rw, rh, depth, area_of(item) / largest, parent))
        if (
            item.is_dir
            and item.has_children
            and depth < levels
            and rh > header + 2 * min_px
            and rw > 2 * min_px
        ):
            _fill(
                tiles,
                item,
                rx + 2,
                ry + header,
                rw - 4,
                rh - header - 2,
                depth + 1,
                len(tiles) - 1,
                levels,
                header,
                min_px,
            )


def hit_test(tiles, x, y):
    """Index of the deepest tile at (x, y), or None."""
    for index in range(len(tiles) - 1, -1, -1):
        tile = tiles[index]
        if tile.x <= x < tile.x + tile.w and tile.y <= y < tile.y + tile.h:
            return index
    return None


def chain(tiles, index):
    """The nodes from the shown folder's item down to tile `index`."""
    nodes = []
    while index is not None:
        nodes.append(tiles[index].node)
        index = tiles[index].parent
    return nodes[::-1]


# -- colours ----------------------------------------------------------------- #

# Files of a kind share a colour; anything else gets one from its extension.
_TYPE_GROUPS = {
    "image": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".webp", ".raw", ".tif", ".tiff"),
    "video": (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v"),
    "audio": (".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma"),
    "archive": (".zip", ".7z", ".rar", ".gz", ".tar", ".iso", ".cab", ".xz", ".bz2"),
    "program": (".exe", ".dll", ".msi", ".sys", ".so", ".dylib", ".app", ".pyd"),
    "document": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md"),
    "disk": (".vhd", ".vhdx", ".vmdk", ".qcow2", ".img", ".pst", ".ost", ".db", ".sqlite"),
}
_GROUP_OF = {ext: group for group, exts in _TYPE_GROUPS.items() for ext in exts}
_GROUP_COLORS = {
    "image": "#2f9e44",
    "video": "#d6336c",
    "audio": "#ae3ec9",
    "archive": "#f08c00",
    "program": "#1c7ed6",
    "document": "#0c8599",
    "disk": "#e8590c",
}
_OTHER_COLORS = ("#748ffc", "#66a80f", "#c2255c", "#5c940d", "#9c36b5", "#1098ad", "#e67700")

_DAY = 86400.0


def tile_color(tile, mode, change_of=None, now=None):
    """The fill for `tile` in colour `mode`. `change_of(folder)` is the
    folder's growth since the last scan, or None (GROWTH only)."""
    node = tile.node
    if node is None:
        return COLORS["border"]
    if mode == TYPE:
        return _type_color(node)
    if mode == AGE:
        return _age_color(node.mtime, time.time() if now is None else now)
    if mode == GROWTH:
        return _growth_color(node, change_of)
    return heat_color(tile.share)


def type_group(name):
    """A file's kind ("image", "video", ...) by extension, or None."""
    return _GROUP_OF.get(os.path.splitext(name)[1].lower())


def _type_color(node):
    if node.is_dir:
        return COLORS["bg2"]
    group = type_group(node.name)
    if group:
        return _GROUP_COLORS[group]
    ext = os.path.splitext(node.name)[1].lower()
    return _OTHER_COLORS[zlib.crc32(ext.encode()) % len(_OTHER_COLORS)]


def _age_color(mtime, now):
    """New is the accent colour, a year or more untouched is the quiet end
    of the heat scale, and in between blends from one to the other."""
    if not mtime:
        return COLORS["heat_low"]
    days = max(0.0, (now - mtime) / _DAY)
    return blend(COLORS["accent"], COLORS["heat_low"], min(1.0, days / 365.0))


def _growth_color(node, change_of):
    """A folder by its own change since the last scan, a file by its
    folder's: grew is warm, shrank is green, unknown or unchanged is quiet."""
    folder = node if node.is_dir else node.parent
    change = change_of(folder) if change_of is not None else None
    if not change or not folder.size:
        return COLORS["heat_low"]
    previous = max(1, folder.size - change)
    fraction = min(1.0, abs(change) / previous / 0.5)  # +50% or more is the far end
    end = COLORS["heat_high"] if change > 0 else COLORS["good"]
    return blend(COLORS["heat_low"], end, 0.35 + 0.65 * fraction)


def blend(start, end, t):
    """The colour `t` (0..1) of the way from hex `start` to hex `end`."""
    a = [int(start[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(end[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(a, b))
