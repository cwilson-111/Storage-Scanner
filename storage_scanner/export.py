"""Writes a scanned Node tree out as JSON or CSV — shared by the headless CLI
(`--cli --format json|csv`) and the GUI's Tools ▸ Export Results…, so both
produce byte-for-byte the same file shapes.
"""

import csv
import json

from storage_scanner.serialization import node_to_dict

FORMATS = ("json", "csv")

CSV_FIELDS = (
    "path", "name", "is_dir", "size", "alloc_size", "file_count", "mtime",
    "is_link", "hardlink_dup", "is_cloud_placeholder", "error",
)


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
        writer.writerow([getattr(n, field) for field in CSV_FIELDS])


def export_to_file(node, path, fmt):
    """Writes the whole tree to path. Raises ValueError for an unknown format
    and OSError if the file can't be written."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}")

    write = write_json if fmt == "json" else write_csv
    newline = "" if fmt == "csv" else None

    with open(path, "w", newline=newline, encoding="utf-8") as f:
        write(node, f)
