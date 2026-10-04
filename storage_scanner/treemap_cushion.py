"""Cushion shading for the treemap pane (ui/treemap_pane.py): the tiles
painted into one image, each a low pillow lit from the top left, so the
items of a folder share its bulge and the nesting reads without outlines.
It's van Wijk and van de Wetering's cushion treemap with WinDirStat's ridge
height, falloff per level and light; the ambient light is higher, so the
darkest edges keep their colour. A flat stretch shows the fill exactly. No
Tk here: render() returns a PPM image that a Tk PhotoImage reads as it is.

A tile's surface is one parabolic ridge per level, its own and each of
its folders', so its slope across depends only on x, its slope down only
on y, and the light at a pixel only on those two slopes. That is what makes
pure Python fast enough: a tile's line of slopes across is worked out once,
and each of its rows is a single bytes.translate of that line through a
table of the tile's colour under every slope across at that row's slope
down, with no per-pixel Python at all.
"""

import math
from collections import deque
from functools import cache, lru_cache

HEIGHT = 0.38  # a top-level tile's ridge: its edges slope 4 * HEIGHT
SCALE = 0.91  # each level down, the ridge is this much lower
AMBIENT = 0.3  # how bright a pixel facing away from the light still is

_NORM = math.sqrt(1 + 1 + 10 * 10)
_LX, _LY, _LZ = -1 / _NORM, -1 / _NORM, 10 / _NORM  # top left, mostly above

# Slopes fall into _BAND bins, evenly spaced by angle, and light into _BAND
# levels. A pixel's red, green and blue bytes hold its bin across offset by
# 0, _BAND and 2 * _BAND, so one 256-byte table per row turns all three into
# light levels, and one per tile turns those into its shaded colour.
_BAND = 85
_FLAT = _BAND // 2  # the bin of a level stretch

# Slopes are looked up _RES to a unit. However deep the nesting, a pixel's
# slope is under 4 * HEIGHT / (1 - SCALE) = 16.9: each ridge's edges slope 4
# times its height, and a pixel's centre is inside every tile it's painted in.
_RES = 128
_SLOPE_MAX = 20
_ANGLE_STEP = math.atan(_SLOPE_MAX) / _FLAT

_GREEN = bytes(min(255, i + _BAND) for i in range(256))
_BLUE = bytes(min(255, i + 2 * _BAND) for i in range(256))


def light(slope_x, slope_y):
    """The cosine between the light and the normal of a surface rising
    `slope_x` to the right and `slope_y` downwards: 1 facing the light,
    0 (or less) facing away from it."""
    lit = _LZ - _LX * slope_x - _LY * slope_y
    return lit / math.sqrt(1 + slope_x * slope_x + slope_y * slope_y)


def _level(slope_x, slope_y):
    return round(max(0.0, min(1.0, light(slope_x, slope_y))) * (_BAND - 1))


@cache
def _slope_bins():
    """The bin of every slope from -_SLOPE_MAX to _SLOPE_MAX."""
    return bytes(
        _FLAT + round(math.atan(k / _RES) / _ANGLE_STEP)
        for k in range(-_SLOPE_MAX * _RES, _SLOPE_MAX * _RES + 1)
    )


@cache
def _row_tables():
    """For each bin down, the translate table from a line's bytes (bin
    across, plus its channel's offset) to light levels (plus the same)."""
    slopes = [math.tan((b - _FLAT) * _ANGLE_STEP) for b in range(_BAND)]
    tables = []
    for slope_y in slopes:
        levels = [_level(slope_x, slope_y) for slope_x in slopes]
        channels = (bytes(level + offset for level in levels) for offset in (0, _BAND, 2 * _BAND))
        tables.append(b"".join(channels) + bytes(256 - 3 * _BAND))
    return tables


@cache
def _channel(value):
    """One colour channel at every light level, `value` where it's flat."""
    flat = AMBIENT + (1 - AMBIENT) * _level(0.0, 0.0) / (_BAND - 1)
    return bytes(
        min(255, round(value * (AMBIENT + (1 - AMBIENT) * level / (_BAND - 1)) / flat))
        for level in range(_BAND)
    )


@lru_cache(maxsize=4096)
def _table(fill):
    """The translate table from light levels to `fill` ("#rrggbb"), shaded."""
    red, green, blue = bytes.fromhex(fill[1:7])
    return _channel(red) + _channel(green) + _channel(blue) + bytes(256 - 3 * _BAND)


def _surface(tiles, index, surfaces):
    """(ax, bx, ay, by): tile `index`'s surface z = ax x² + bx x + ay y² + by y,
    its parent's plus its own ridge."""
    tile = tiles[index]
    ax, bx, ay, by = surfaces[tile.parent] if tile.parent is not None else (0.0, 0.0, 0.0, 0.0)
    height = 4 * HEIGHT * SCALE ** (tile.depth - 1)
    fx, fy = height / tile.w, height / tile.h
    return ax - fx, bx + fx * (2 * tile.x + tile.w), ay - fy, by + fy * (2 * tile.y + tile.h)


def _bins(a, b, start, stop):
    """The slope bin at each pixel centre from `start` to `stop` along one
    axis of the surface a p² + b p."""
    origin = (_SLOPE_MAX + 2 * a * (start + 0.5) + b) * _RES + 0.5
    step = 2 * a * _RES
    offsets = map(origin.__add__, map(step.__mul__, range(stop - start)))
    return bytes(map(_slope_bins().__getitem__, map(int, offsets)))


def _span(start, length, limit):
    return max(0, min(limit, round(start))), max(0, min(limit, round(start + length)))


def render(tiles, fills, width, height, background):
    """The tiles (treemap_model.Tile, parents first), each in its fill,
    painted in order over `background` (all "#rrggbb") as a binary PPM of
    width x height."""
    stride = 3 * width
    image = bytearray(bytes.fromhex(background[1:7]) * (width * height))
    row_tables = _row_tables()
    surfaces = []  # type: ignore[var-annotated]
    for index, fill in enumerate(fills):
        surfaces.append(_surface(tiles, index, surfaces))
        tile = tiles[index]
        x0, x1 = _span(tile.x, tile.w, width)
        y0, y1 = _span(tile.y, tile.h, height)
        if x1 <= x0 or y1 <= y0:
            continue
        ax, bx, ay, by = surfaces[index]
        across = _bins(ax, bx, x0, x1)
        down = _bins(ay, by, y0, y1)
        line = bytearray(3 * len(across))
        line[0::3] = across
        line[1::3] = across.translate(_GREEN)
        line[2::3] = across.translate(_BLUE)
        table = _table(fill)
        lit = {b: row_tables[b].translate(table) for b in set(down)}
        start = (y0 * width + x0) * 3
        stop = start + (y1 - y0) * stride
        end = start + len(line)
        targets = map(slice, range(start, stop, stride), range(end, stop + len(line), stride))
        pixels = map(bytes(line).translate, map(lit.__getitem__, down))
        deque(map(image.__setitem__, targets, pixels), maxlen=0)
    return b"P6\n%d %d\n255\n" % (width, height) + image


def shade(tiles, index, fill, x, y):
    """`fill` as render() leaves it at pixel (x, y) in tile `index`."""
    path = []
    while index is not None:
        path.append(index)
        index = tiles[index].parent
    surfaces = {}  # type: ignore[var-annotated]
    for i in reversed(path):
        surfaces[i] = _surface(tiles, i, surfaces)
    ax, bx, ay, by = surfaces[path[0]]
    px, py = int(x), int(y)
    across = _bins(ax, bx, px, px + 1)[0]
    lit = _row_tables()[_bins(ay, by, py, py + 1)[0]].translate(_table(fill))
    return "#" + bytes((lit[across], lit[_BAND + across], lit[2 * _BAND + across])).hex()
