"""Builds a storage_scanner.models.Node tree out of parsed MFT records.

Pure in-memory graph construction over a list of
storage_scanner.mft_parser.ParsedRecord objects -- no filesystem or ctypes
access, so (like mft_parser.py) this is fully unit-testable with plain,
hand-built fake records; see tests/test_mft_scan.py.

Every hard-link occurrence of a record (one Node per (parent, name) pair in
its `names` list) is attached under its own parent directory -- exactly one
occurrence per record is "primary" and carries the real size/alloc_size,
the rest are marked `hardlink_dup` with size zeroed. This mirrors
storage_scanner.scanner.scan()'s own hard-link handling (its `seen_inodes`
dedup) exactly, so the two engines report identical totals for the same
hard-linked file.
"""

import os

from storage_scanner.models import Node
from storage_scanner.scanner import _rollup

# Every NTFS volume's root directory is always MFT record #5 -- a
# documented, universal on-disk invariant (record 0 is $MFT itself; 1-4 are
# other fixed system metadata files; 5 is "." the volume root).
_ROOT_RECORD_NUMBER = 5

_FRN_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF


def _make_node(record, name, is_primary):
    # A reparse point (junction, symlink, OneDrive cloud-placeholder-style
    # tag) is never traversed regardless of the record's own directory
    # flag -- treated as a leaf, exactly like scanner.py's
    # `is_dir = entry.is_dir(follow_symlinks=False) and not is_reparse`.
    is_dir = record.is_directory and not record.is_reparse_point
    node = Node(path="", name=name, is_dir=is_dir)
    node.mtime = record.mtime
    node.atime = record.atime
    node.is_cloud_placeholder = record.is_cloud_placeholder
    # A cloud placeholder that also carries the reparse bit still renders
    # as a normal file, not a link -- matches scanner.py's
    # `is_link = is_reparse and not is_placeholder`.
    node.is_link = record.is_reparse_point and not record.is_cloud_placeholder
    if is_primary:
        node.size = record.logical_size
        node.alloc_size = record.alloc_size
        node.hardlink_dup = False
    else:
        node.size = 0
        node.alloc_size = 0
        node.hardlink_dup = True
    node.file_count = 0 if is_dir else 1
    return node


def build_tree(records, root_path, root_record_number=_ROOT_RECORD_NUMBER):
    """Build a fully rolled-up Node tree from `records`, rooted at whichever
    record's FRN has record number `root_record_number`.

    `root_path` becomes the root Node's `.path`/`.name` -- MFT records only
    ever carry names and parent linkage, never a real filesystem path, so
    the caller (which knows what volume/subtree was actually requested)
    has to supply it. Naming follows scanner.scan()'s own convention: a
    path ending in a separator (a drive root) uses the full path as its
    name, otherwise the last path component is used.

    Returns (root_node, orphan_count). `orphan_count` counts hard-link
    occurrences whose parent was never reached while walking down from the
    root -- its parent record is missing entirely, its parent chain loops
    back on itself without ever reaching the root, or its parent turned out
    to be a reparse point (never expanded) or an already-visited directory
    FRN (a cycle/duplicate-linked-directory guard -- see below). None of
    these raise; a caller that gets back a nonzero orphan_count can decide
    for itself whether that's tolerable or a reason to fall back to the
    Compatible engine.

    Returns (None, 0) if no record matches `root_record_number` or it isn't
    a real directory -- both mean there's nothing trustworthy to build,
    which should also send the caller to the fallback engine.
    """
    root_record = None
    for record in records:
        if (record.frn & _FRN_RECORD_NUMBER_MASK) == root_record_number:
            root_record = record
            break
    if root_record is None or not root_record.is_directory:
        return None, 0

    # Group every hard-link occurrence by its parent's FRN, deciding once
    # per record which occurrence is "primary". The root's own name entry
    # (which, per NTFS convention, points at itself) is skipped -- it has
    # no place as anyone's child in the tree being built here.
    children_by_parent = {}
    total_occurrences = 0
    for record in records:
        if record.frn == root_record.frn:
            continue
        for index, name_attr in enumerate(record.names):
            children_by_parent.setdefault(name_attr.parent_frn, []).append(
                (record, name_attr, index == 0)
            )
            total_occurrences += 1

    root_path = os.path.abspath(root_path)
    root_name = root_path if root_path.endswith(os.sep) else (
        os.path.basename(root_path) or root_path
    )
    root_node = _make_node(root_record, root_name, is_primary=True)
    root_node.path = root_path

    attached_occurrences = 0
    # Directories are expanded at most once each, however many times a
    # (corrupt) parent chain tries to route back through one -- this is
    # what makes the traversal below immune to cycles no matter how deep
    # or tangled a damaged volume's parent links are.
    visited_dir_frns = {root_record.frn}
    stack = [(root_node, root_record.frn)]
    while stack:
        node, frn = stack.pop()
        for child_record, name_attr, is_primary in children_by_parent.get(frn, []):
            child_node = _make_node(child_record, name_attr.name, is_primary)
            child_node.path = os.path.join(node.path, name_attr.name)
            node.children.append(child_node)
            attached_occurrences += 1

            if child_node.is_dir:
                child_frn = child_record.frn
                if child_frn in visited_dir_frns:
                    # A cycle, or a directory improbably claiming more than
                    # one hard link (invalid on real NTFS) -- either way,
                    # never expand the same directory's contents twice.
                    child_node.error = True
                    continue
                visited_dir_frns.add(child_frn)
                stack.append((child_node, child_frn))

    orphan_count = total_occurrences - attached_occurrences
    _rollup(root_node)
    return root_node, orphan_count
