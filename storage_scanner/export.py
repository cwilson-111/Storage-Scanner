"""Writes a scanned Node tree out as JSON or CSV — shared by the headless CLI
(`--cli --format json|csv`) and the GUI's Tools ▸ Export Results…, so both
produce byte-for-byte the same file shapes.
"""

import csv
import json
from datetime import datetime

from storage_scanner.serialization import node_to_dict

FORMATS = ("json", "csv")

CSV_FIELDS = (
    "path",
    "name",
    "is_dir",
    "size",
    "alloc_size",
    "file_count",
    "mtime",
    "is_link",
    "hardlink_dup",
    "is_cloud_placeholder",
    "error",
    "modified",
    "accessed",
)

# A spreadsheet runs a cell starting with one of these as a formula (a file
# named "=HYPERLINK(…)" would be a live link in Excel).
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _local_time(epoch):
    """ISO local time for an epoch-seconds value, "" when unknown (0)."""
    return datetime.fromtimestamp(epoch).isoformat(sep=" ", timespec="seconds") if epoch else ""


def _cell(value):
    if isinstance(value, str) and value.startswith(_FORMULA_STARTS):
        return "'" + value
    return value


def _csv_row(node):
    """One row of CSV_FIELDS. `mtime` stays epoch seconds for scripts that
    already read it; `modified` and `accessed` are the same kind of times,
    readable."""
    values = {field: getattr(node, field) for field in CSV_FIELDS[:-2]}
    values["modified"] = _local_time(node.mtime)
    values["accessed"] = _local_time(node.atime)
    return [_cell(values[field]) for field in CSV_FIELDS]


def iter_nodes(node):
    """Every node in the tree, parents before their children. Iterative, so a
    very deep folder tree can't hit the recursion limit."""
    stack = [node]

    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def write_json(node, out):
    json.dump(node_to_dict(node), out)
    out.write("\n")


def write_csv(node, out):
    writer = csv.writer(out)
    writer.writerow(CSV_FIELDS)

    for n in iter_nodes(node):
        writer.writerow(_csv_row(n))


def export_to_file(node, path, fmt):
    """Writes the whole tree to path. Raises ValueError for an unknown format
    and OSError if the file can't be written."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}")

    write = write_json if fmt == "json" else write_csv
    newline = "" if fmt == "csv" else None

    with open(path, "w", newline=newline, encoding="utf-8") as f:
        write(node, f)
