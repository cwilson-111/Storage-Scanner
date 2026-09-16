"""Builds a storage_scanner.models.Node tree out of parsed MFT records.

Pure in-memory graph construction over a list of
storage_scanner.mft_parser.ParsedRecord objects -- no filesystem or ctypes
access, so (like mft_parser.py) this is fully unit-testable with plain,
hand-built fake records; see tests/test_mft_scan.py.

Every hard-link occurrence of a record (one Node per (parent, name) pair in
its `names` list) is attached under its own parent directory, each carrying
its own full, undeduped size -- build_tree() deliberately does NOT decide
which occurrence is "primary" here. That decision is deferred to
finalize_subtree(), applied only to whatever subtree the caller actually
asked for (see storage_scanner.turbo_scan.find_subtree_node), because
deciding it globally across the whole volume can zero out a file's size
within the very folder being looked at just because its OTHER hard-linked
occurrence (e.g. Windows system files also linked into C:\\Windows\\WinSxS)
happened to be picked as primary instead -- scanner.scan()'s own dedup can
never make this mistake, because it only ever sees what it actually
walked. This whole-volume-vs-requested-subtree scope mismatch was a real
bug the Turbo Scan validation gate caught on a real machine (files under
C:\\Windows\\Fonts, also hard-linked into WinSxS, showing up zeroed).
"""

import os

from storage_scanner.models import Node
from storage_scanner.scanner import _rollup

# Every NTFS volume's root directory is always MFT record #5 -- a
# documented, universal on-disk invariant (record 0 is $MFT itself; 1-4 are
# other fixed system metadata files; 5 is "." the volume root).
_ROOT_RECORD_NUMBER = 5

_FRN_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF


def _make_node(record, name):
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
    # Full, undeduped size -- see module docstring for why hard-link
    # dedup is deferred to finalize_subtree() rather than decided here.
    node.size = record.logical_size
    node.alloc_size = record.alloc_size
    node.hardlink_dup = False
    node.file_count = 0 if is_dir else 1
    return node


def build_tree(records, root_path, root_record_number=_ROOT_RECORD_NUMBER):
    """Build a Node tree from `records`, rooted at whichever record's FRN
    has record number `root_record_number`. Every node's size/alloc_size
    is real and undeduped, and nothing is rolled up yet -- call
    finalize_subtree() on whatever part of this tree the caller actually
    wants (see find_subtree_node) before trusting its aggregated numbers.

    `root_path` becomes the root Node's `.path`/`.name` -- MFT records only
    ever carry names and parent linkage, never a real filesystem path, so
    the caller (which knows what volume/subtree was actually requested)
    has to supply it. Naming follows scanner.scan()'s own convention: a
    path ending in a separator (a drive root) uses the full path as its
    name, otherwise the last path component is used.

    Returns (root_node, orphan_count, frn_by_node_id). `orphan_count`
    counts hard-link occurrences whose parent was never reached while
    walking down from the root -- its parent record is missing entirely,
    its parent chain loops back on itself without ever reaching the root,
    or its parent turned out to be a reparse point (never expanded) or an
    already-visited directory FRN (a cycle/duplicate-linked-directory
    guard -- see below). None of these raise; a caller that gets back a
    nonzero orphan_count can decide for itself whether that's tolerable or
    a reason to fall back to the Compatible engine.

    `frn_by_node_id` maps `id(node)` to the FRN of the record it came from,
    for every node in the tree (root included) -- the side channel
    finalize_subtree() needs to regroup hard-link occurrences, since
    Node's own slots deliberately never carry an FRN.

    Returns (None, 0, {}) if no record matches `root_record_number` or it
    isn't a real directory -- both mean there's nothing trustworthy to
    build, which should also send the caller to the fallback engine.
    """
    root_record = None
    for record in records:
        if (record.frn & _FRN_RECORD_NUMBER_MASK) == root_record_number:
            root_record = record
            break
    if root_record is None or not root_record.is_directory:
        return None, 0, {}

    # Group every hard-link occurrence by its parent's FRN. The root's own
    # name entry (which, per NTFS convention, points at itself) is skipped
    # -- it has no place as anyone's child in the tree being built here.
    children_by_parent = {}
    total_occurrences = 0
    for record in records:
        if record.frn == root_record.frn:
            continue
        for name_attr in record.names:
            children_by_parent.setdefault(name_attr.parent_frn, []).append(
                (record, name_attr)
            )
            total_occurrences += 1

    root_path = os.path.abspath(root_path)
    root_name = root_path if root_path.endswith(os.sep) else (
        os.path.basename(root_path) or root_path
    )
    root_node = _make_node(root_record, root_name)
    root_node.path = root_path
    frn_by_node_id = {id(root_node): root_record.frn}

    attached_occurrences = 0
    # Directories are expanded at most once each, however many times a
    # (corrupt) parent chain tries to route back through one -- this is
    # what makes the traversal below immune to cycles no matter how deep
    # or tangled a damaged volume's parent links are.
    visited_dir_frns = {root_record.frn}
    stack = [(root_node, root_record.frn)]
    while stack:
        node, frn = stack.pop()
        for child_record, name_attr in children_by_parent.get(frn, []):
            child_node = _make_node(child_record, name_attr.name)
            child_node.path = os.path.join(node.path, name_attr.name)
            node.children.append(child_node)
            frn_by_node_id[id(child_node)] = child_record.frn
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
    return root_node, orphan_count, frn_by_node_id


def finalize_subtree(subtree_root, frn_by_node_id):
    """Redo hard-link dedup scoped to just `subtree_root`'s own tree, then
    roll up size/alloc_size/file_count. Call this on whatever subtree
    build_tree()'s caller actually cares about (see find_subtree_node) --
    never on the whole volume, and never trust a subtree's aggregated
    numbers before calling this on it.

    A record whose FRN appears more than once within `subtree_root` (e.g.
    two hard-linked names both inside the requested folder) still gets
    exactly one primary, full-sized occurrence -- the first one found
    while walking the subtree, a deterministic (if somewhat arbitrary,
    same as scanner.py's own discovery-order-dependent choice) tie-break.
    Every other occurrence of that FRN, still within this same subtree, is
    zeroed as a duplicate. An FRN that appears only once within this
    subtree is never zeroed, even if the same file has other hard-linked
    occurrences elsewhere in the volume outside this subtree entirely --
    see module docstring for why.
    """
    nodes_by_frn = {}
    stack = [subtree_root]
    while stack:
        node = stack.pop()
        frn = frn_by_node_id.get(id(node))
        if frn is not None:
            nodes_by_frn.setdefault(frn, []).append(node)
        stack.extend(node.children)

    for nodes in nodes_by_frn.values():
        primary, *duplicates = nodes
        primary.hardlink_dup = False
        for duplicate in duplicates:
            duplicate.hardlink_dup = True
            duplicate.size = 0
            duplicate.alloc_size = 0

    _rollup(subtree_root)
    return subtree_root
