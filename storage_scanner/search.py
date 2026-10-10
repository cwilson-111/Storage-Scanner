"""Search and filter the in-memory scanned tree.

Pure, Tkinter-free logic (easy to unit test) — storage_scanner/ui/search_window.py
wires this up to an actual results window.
"""

import heapq
import os
import re
from typing import NamedTuple

from storage_scanner.models import FileNode

_SIZE_UNITS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
    "tb": 1024**4,
}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


def parse_size(text):
    """Parse a human size string ("500", "500mb", "2.5 GB") into a byte count.

    Returns None for blank input. Raises ValueError for anything else that
    doesn't parse, so callers can surface a clear message to the user.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None

    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"Can't parse size: {text!r}")

    number, unit = match.groups()
    unit = (unit or "b").lower()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"Unknown size unit: {unit!r}")

    return int(float(number) * _SIZE_UNITS[unit])


class SearchResult(NamedTuple):
    nodes: list  # the `limit` largest matches, largest first
    matched: int  # how many matched, listed or not
    size: int  # the bytes of all of them


def largest_matches(root, limit, **filters):
    """The `limit` largest descendants of `root` matching every filter (see
    _matches), largest first, with the count and bytes of every match. Only
    those `limit` become FileNode views, so a filter that matches a whole
    drive (a million files) costs a walk, not a million objects and list
    rows."""
    count = size = 0

    def counted():
        nonlocal count, size
        for match in _matches(root, **filters):
            count += 1
            size += match[0]
            yield match

    top = heapq.nlargest(limit, counted(), key=lambda match: match[0])
    return SearchResult([_node(match) for match in top], count, size)


def _node(match):
    _size, folder, index = match
    return folder if index is None else FileNode(folder, index)


def _matches(
    root,
    name_query=None,
    extensions=None,
    min_size=None,
    max_size=None,
    mtime_after=None,
    mtime_before=None,
    include_dirs=True,
    include_files=True,
):
    """(size, folder, row) for each descendant of `root` matching every
    given filter: row is a file's index in `folder`, or None when `folder`
    itself matched.

    `root` itself is never included — only its descendants. Every filter
    that is None/empty is treated as "no constraint"; filters combine with
    AND. `extensions`, if given, is an iterable of extensions without the
    leading dot (case-insensitive) and only ever matches files. Files are
    checked straight from their folder's columns.
    """
    name_query = name_query.strip().lower() if name_query else None
    ext_set = (
        {e.strip().lower().lstrip(".") for e in extensions if e.strip()} if extensions else None
    )

    def matches(name, size, mtime):
        if name_query and name_query not in name.lower():
            return False
        if min_size is not None and size < min_size:
            return False
        if max_size is not None and size > max_size:
            return False
        if mtime_after is not None and mtime < mtime_after:
            return False
        return mtime_before is None or mtime <= mtime_before

    if not root.is_dir:
        return
    want_dirs = include_dirs and ext_set is None
    stack = [root]
    while stack:
        folder = stack.pop()
        stack.extend(folder.dirs)
        if want_dirs:
            for d in folder.dirs:
                if matches(d.name, d.size, d.mtime):
                    yield d.size, d, None
        if not include_files:
            continue
        names, sizes, mtimes = folder.file_names, folder.file_sizes, folder.file_mtimes
        for i in folder.file_rows():
            name = names[i]
            if ext_set is not None and os.path.splitext(name)[1].lstrip(".").lower() not in ext_set:
                continue
            if matches(name, sizes[i], mtimes[i]):
                yield sizes[i], folder, i
