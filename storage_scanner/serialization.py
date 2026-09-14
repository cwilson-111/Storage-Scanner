"""Node <-> plain-dict conversion, for passing a scanned tree as JSON.

Needed by the elevated-scan helper (storage_scanner/priv_scan_cli.py): the
privileged process can't share memory with the GUI process, so the tree it
scans has to cross that boundary as JSON on stdout.
"""

from storage_scanner.models import Node


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


def dict_to_node(d):
    node = Node(d["path"], d["name"], d["is_dir"])
    node.size = d["size"]
    node.file_count = d["file_count"]
    node.error = d["error"]
    node.is_link = d["is_link"]
    node.hardlink_dup = d["hardlink_dup"]
    node.mtime = d["mtime"]
    node.atime = d["atime"]
    node.alloc_size = d["alloc_size"]
    node.is_cloud_placeholder = d["is_cloud_placeholder"]
    node.children = [dict_to_node(child) for child in d["children"]]
    return node
