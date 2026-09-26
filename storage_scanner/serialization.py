"""Node <-> plain-dict conversion, for passing a scanned tree as JSON.

Needed by the elevated-scan helper (storage_scanner/priv_scan_cli.py): the
privileged process can't share memory with the GUI process, so the tree it
scans has to cross that boundary as JSON on stdout. Every file gets a dict
of its own, the same shape as a folder's, so the JSON export
(storage_scanner/export.py) reads the same whichever way the tree is held.
"""

from storage_scanner.models import Node, detached_file, row_flags


def node_to_dict(node):
    return {
        "path": node.path,
        "name": node.name,
        "is_dir": node.is_dir,
        "size": node.size,
        "file_count": node.file_count,
        "error": node.error,
        "is_link": node.is_link,
        "hardlink_dup": node.hardlink_dup,
        "mtime": node.mtime,
        "atime": node.atime,
        "alloc_size": node.alloc_size,
        "is_cloud_placeholder": node.is_cloud_placeholder,
        "children": [node_to_dict(child) for child in node.children],
    }


def _flags(d):
    return row_flags(d["error"], d["is_link"], d["hardlink_dup"], d["is_cloud_placeholder"])


def dict_to_node(d):
    """The tree node_to_dict() described: a Node, or a FileNode when the
    scan target was a single file."""
    if not d["is_dir"]:
        return detached_file(
            d["path"], d["size"], d["alloc_size"], d["mtime"], d["atime"], _flags(d)
        )
    return _dict_to_folder(d)


def _dict_to_folder(d):
    node = Node(d["path"], d["name"])
    node.size = d["size"]
    node.alloc_size = d["alloc_size"]
    node.file_count = d["file_count"]
    node.error = d["error"]
    node.mtime = d["mtime"]
    node.atime = d["atime"]
    node.is_cloud_placeholder = d["is_cloud_placeholder"]
    for child in d["children"]:
        if child["is_dir"]:
            node.dirs.append(_dict_to_folder(child))
        else:
            node.add_file(
                child["name"],
                child["size"],
                child["alloc_size"],
                child["mtime"],
                child["atime"],
                _flags(child),
            )
    return node
