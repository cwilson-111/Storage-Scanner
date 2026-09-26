"""Builds a storage_scanner.models.Node tree out of parsed MFT records.

Pure in-memory graph construction over a list of
storage_scanner.mft_parser.ParsedRecord objects -- no filesystem or ctypes
access, so (like mft_parser.py) this is fully unit-testable with plain,
hand-built fake records; see tests/test_mft_scan.py.

Every hard-link occurrence of a record (one file row per (parent, name)
pair in its `names` list) is attached under its own parent directory, each
carrying its own full, undeduped size -- build_tree() deliberately does NOT decide
which occurrence is "primary" here. That decision is deferred to
finalize_subtree(), applied only to whatever subtree the caller actually
asked for (see storage_scanner.turbo_read.find_subtree_node), because
deciding it globally across the whole volume can zero out a file's size
within the very folder being looked at just because its OTHER hard-linked
occurrence (e.g. Windows system files also linked into C:\\Windows\\WinSxS)
happened to be picked as primary instead -- scanner.scan()'s own dedup can
never make this mistake, because it only ever sees what it actually
walked. This whole-volume-vs-requested-subtree scope mismatch was a real
bug the Turbo Scan validation gate caught on a real machine (files under
C:\\Windows\\Fonts, also hard-linked into WinSxS, showing up zeroed).

Similarly, build_tree() always treats a reparse point as a leaf, never
traversed -- correct for one encountered as a *child*, but not for the
node a caller actually asked to scan (find_subtree_node's result): a
requested folder that happens to be a junction/symlink should still be
followed and its real contents shown, matching scanner.scan()'s own root-
vs-child asymmetry. See reroot_if_reparse_point() for that one-node
exception, applied by the caller after find_subtree_node(), before
finalize_subtree().
"""

import os

from storage_scanner.models import (
    FLAG_CLOUD_PLACEHOLDER,
    FLAG_HARDLINK_DUP,
    FLAG_LINK,
    Node,
    detached_file,
    iter_folders,
)
from storage_scanner.scanner import _rollup

# Every NTFS volume's root directory is always MFT record #5 -- a
# documented, universal on-disk invariant (record 0 is $MFT itself; 1-4 are
# other fixed system metadata files; 5 is "." the volume root).
_ROOT_RECORD_NUMBER = 5

_FRN_RECORD_NUMBER_MASK = 0x0000FFFFFFFFFFFF


def _folder(record, path, name):
    """The Node for a directory record. A reparse point (junction,
    symlink) is never one of these below the scan root -- see _is_folder."""
    node = Node(path, name)
    node.mtime = record.mtime
    node.atime = record.atime
    node.is_cloud_placeholder = record.is_cloud_placeholder
    # A directory's own record size, which roll-up then adds to.
    node.size = record.logical_size
    node.alloc_size = record.alloc_size
    return node


def _is_folder(record):
    # A reparse point (junction, symlink, OneDrive cloud-placeholder-style
    # tag) is never traversed regardless of the record's own directory
    # flag -- recorded as a file row, exactly like scanner.py's
    # `is_dir = entry.is_dir(follow_symlinks=False) and not is_reparse`.
    # BUT only when it's encountered as a *child* during traversal: the
    # requested scan root itself is followed, like scanner.py's root
    # handling (`os.path.isdir(path)`) -- see reroot_if_reparse_point for
    # where this asymmetry actually gets exercised.
    return record.is_directory and not record.is_reparse_point


def _row_flags(record):
    # A cloud placeholder that also carries the reparse bit still renders
    # as a normal file, not a link -- matches scanner.py.
    if record.is_cloud_placeholder:
        return FLAG_CLOUD_PLACEHOLDER
    return FLAG_LINK if record.is_reparse_point else 0


def _add_row(folder, record, name):
    # Full, undeduped size -- see module docstring for why hard-link dedup
    # is deferred to finalize_subtree() rather than decided here.
    return folder.add_file(
        name, record.logical_size, record.alloc_size, record.mtime, record.atime, _row_flags(record)
    )


def file_node(record, path):
    """The FileNode for a single-file scan target: exactly the row
    build_tree() attaches for `record` under its parent (a link to a file
    stays a link -- reroot_if_reparse_point only ever follows directories)."""
    return detached_file(
        path, record.logical_size, record.alloc_size, record.mtime, record.atime, _row_flags(record)
    )


def build_tree(records, root_path, root_record_number=_ROOT_RECORD_NUMBER):
    """Build a Node tree from `records`, rooted at whichever record's FRN
    has record number `root_record_number`. Every file row's size/alloc
    size is real and undeduped, and nothing is rolled up yet -- call
    finalize_subtree() on whatever part of this tree the caller actually
    wants (see find_subtree_node) before trusting its aggregated numbers.

    `root_path` becomes the root Node's `.path`/`.name` -- MFT records only
    ever carry names and parent linkage, never a real filesystem path, so
    the caller (which knows what volume/subtree was actually requested)
    has to supply it. Naming follows scanner.scan()'s own convention: a
    path ending in a separator (a drive root) uses the full path as its
    name, otherwise the last path component is used.

    Returns (root_node, orphan_count, row_frns). `orphan_count`
    counts hard-link occurrences whose parent was never reached while
    walking down from the root -- its parent record is missing entirely,
    its parent chain loops back on itself without ever reaching the root,
    or its parent turned out to be a reparse point (never expanded) or an
    already-visited directory FRN (a cycle/duplicate-linked-directory
    guard -- see below). None of these raise; a caller that gets back a
    nonzero orphan_count can decide for itself whether that's tolerable or
    a reason to fall back to the Compatible engine.

    `row_frns` maps a folder Node to [(row index, FRN), ...] for each of
    its file rows whose record has more than one name (a hard link, which
    finalize_subtree() regroups) or is a reparse point (which
    reroot_if_reparse_point() may follow) -- the side channel those need,
    since a row deliberately carries no FRN. Every other row's record has
    exactly one occurrence, so there's nothing to regroup.

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
            children_by_parent.setdefault(name_attr.parent_frn, []).append((record, name_attr))
            total_occurrences += 1

    root_path = os.path.abspath(root_path)
    root_name = (
        root_path if root_path.endswith(os.sep) else (os.path.basename(root_path) or root_path)
    )
    # The requested root is followed even if it's a reparse point, and is
    # never flagged as a link -- matching scanner.py's root Node.
    root_node = _folder(root_record, root_path, root_name)
    row_frns = {}

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
            attached_occurrences += 1
            name = name_attr.name
            if not _is_folder(child_record):
                index = _add_row(node, child_record, name)
                if child_record.is_reparse_point or len(child_record.names) > 1:
                    row_frns.setdefault(node, []).append((index, child_record.frn))
                continue

            child_node = _folder(child_record, os.path.join(node.path, name), name)
            node.dirs.append(child_node)
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
    return root_node, orphan_count, row_frns


def _row_frn(row_frns, file_node):
    for index, frn in row_frns.get(file_node.parent, ()):
        if index == file_node.index:
            return frn
    return None


def reroot_if_reparse_point(node, target_path, records, row_frns):
    """If `node` (whatever find_subtree_node() located) is a reparse point
    that build_tree() left as an unexpanded file row, rebuild it as a fresh
    root and return that instead -- matching scanner.scan()'s own root-vs-
    child asymmetry: a reparse point is only ever an unfollowable leaf
    when encountered as a *child* during traversal, never when it's the
    requested scan root itself (scanner.py's root handling just uses
    os.path.isdir(), which transparently follows a reparse point; only its
    per-child walk excludes them). Any reparse point *inside* the rebuilt
    subtree is still correctly left as a leaf -- build_tree()'s normal
    per-child rule takes back over one level down; only the single
    outermost node passed in here ever gets the root treatment.

    No-op (returns `node` unchanged) if it isn't actually a reparse point,
    its FRN can't be resolved, or the underlying record turns out not to
    be a directory after all (a reparse point can just as easily be a
    symlink to a *file*, which scanner.py's root handling wouldn't follow
    as a directory either -- `os.path.isdir()` would be False for it).

    Mutates `row_frns` in place to fold in the rebuilt subtree's rows, so
    a later finalize_subtree() call on the returned node still resolves
    hard-link scoping correctly.
    """
    if not node.is_link:
        return node
    frn = _row_frn(row_frns, node)
    if frn is None:
        return node
    original_record = next((r for r in records if r.frn == frn), None)
    if original_record is None or not original_record.is_directory:
        return node

    record_number = frn & _FRN_RECORD_NUMBER_MASK
    new_root, _orphan_count, new_row_frns = build_tree(
        records,
        root_path=target_path,
        root_record_number=record_number,
    )
    if new_root is None:
        return node

    row_frns.update(new_row_frns)
    return new_root


def finalize_subtree(subtree_root, row_frns):
    """Redo hard-link dedup scoped to just `subtree_root`'s own tree, then
    roll up size/alloc_size/file_count. Call this on whatever subtree
    build_tree()'s caller actually cares about (see find_subtree_node) --
    never on the whole volume, and never trust a subtree's aggregated
    numbers before calling this on it. A single file (a FileNode) has
    nothing to dedup or roll up and comes back as it is.

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
    if not subtree_root.is_dir:
        return subtree_root

    rows_by_frn = {}
    for folder in iter_folders(subtree_root):
        for index, frn in row_frns.get(folder, ()):
            rows_by_frn.setdefault(frn, []).append((folder, index))

    for rows in rows_by_frn.values():
        (folder, index), *duplicates = rows
        folder.file_flags[index] &= ~FLAG_HARDLINK_DUP
        for folder, index in duplicates:
            folder.file_flags[index] |= FLAG_HARDLINK_DUP
            folder.file_sizes[index] = 0
            folder.file_allocs[index] = 0

    _rollup(subtree_root)
    return subtree_root
