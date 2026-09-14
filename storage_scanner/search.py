"""Search and filter the in-memory scanned tree.

Pure, Tkinter-free logic (easy to unit test) — storage_scanner/ui/search_window.py
wires this up to an actual results window.
"""

import os
import re

_SIZE_UNITS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024 ** 2,
    "gb": 1024 ** 3,
    "tb": 1024 ** 4,
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
    leading dot (case-insensitive) and only ever matches files.
    """
    name_query = name_query.strip().lower() if name_query else None
    ext_set = {e.strip().lower().lstrip(".") for e in extensions if e.strip()} if extensions else None

    results = []
    stack = list(root.children)
    while stack:
        node = stack.pop()
        if node.is_dir:
            stack.extend(node.children)
            if not include_dirs:
                continue
        elif not include_files:
            continue

        if name_query and name_query not in node.name.lower():
            continue
        if ext_set is not None:
            if node.is_dir:
                continue
            ext = os.path.splitext(node.name)[1].lstrip(".").lower()
            if ext not in ext_set:
                continue
        if min_size is not None and node.size < min_size:
            continue
        if max_size is not None and node.size > max_size:
            continue
        if mtime_after is not None and node.mtime < mtime_after:
            continue
        if mtime_before is not None and node.mtime > mtime_before:
            continue

        results.append(node)

    return results
