"""Search and filter the in-memory scanned tree.

Pure, Tkinter-free logic (easy to unit test) — storage_scanner/ui/search_window.py
wires this up to an actual results window.
"""

import os
import re

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


def filter_nodes(
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
    """Return a flat list of descendants of `root` matching every given filter.

    `root` itself is never included — only its descendants. Every filter
    that is None/empty is treated as "no constraint"; filters combine with
    AND. `extensions`, if given, is an iterable of extensions without the
    leading dot (case-insensitive) and only ever matches files. Files are
    checked straight from their folder's columns; a FileNode is made only
    for a match.
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

    results = []
    if not root.is_dir:
        return results
    want_dirs = include_dirs and ext_set is None
    stack = [root]
    while stack:
        folder = stack.pop()
        stack.extend(folder.dirs)
        if want_dirs:
            results.extend(d for d in folder.dirs if matches(d.name, d.size, d.mtime))
        if not include_files:
            continue
        names, sizes, mtimes = folder.file_names, folder.file_sizes, folder.file_mtimes
        for i in folder.file_rows():
            name = names[i]
            if ext_set is not None and os.path.splitext(name)[1].lstrip(".").lower() not in ext_set:
                continue
            if matches(name, sizes[i], mtimes[i]):
                results.append(FileNode(folder, i))

    return results
