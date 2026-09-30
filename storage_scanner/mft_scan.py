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

Similarly, build_tree() always treats a link (junction, symbolic link,
mount point -- ParsedRecord.is_link) as a leaf, never traversed. A link
that is itself the folder a caller asked to scan can't be read from the
MFT at all: its own directory index is always empty (NTFS refuses to make
a non-empty folder a junction), and what it shows lives under its target,
possibly on another volume. refuse_linked_folder() turns that case into an
error, so the caller falls back to the Compatible engine, which follows a
linked scan root like any other. A cloud-sync folder (OneDrive) is a
reparse point too, but not a link: it holds its own files and is walked.
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
    """The Node for a directory record. A link (junction, symlink) is
    never one of these -- see _is_folder."""
    node = Node(path, name)
    node.mtime = record.mtime
    node.atime = record.atime
    node.is_cloud_placeholder = record.is_cloud_placeholder
    # A directory's own record size, which roll-up then adds to.
    node.size = record.logical_size
    node.alloc_size = record.alloc_size
    return node


def _is_folder(record):
    # A link (junction, symlink, mount point) is never traversed regardless
    # of the record's own directory flag -- recorded as a file row, exactly
    # like scanner.py's `is_dir = entry.is_dir(follow_symlinks=False) and
    # not is_reparse`. A OneDrive folder is walked: its reparse bit isn't a
    # link's (see ParsedRecord.is_link), and scanner.py never even sees it.
    return record.is_directory and not record.is_link


def _row_flags(record):
    # A cloud placeholder that also carries the reparse bit still renders
    # as a normal file, not a link -- matches scanner.py.
    if record.is_cloud_placeholder:
        return FLAG_CLOUD_PLACEHOLDER
    return FLAG_LINK if record.is_link else 0


def _add_row(folder, record, name):
    # Full, undeduped size -- see module docstring for why hard-link dedup
    # is deferred to finalize_subtree() rather than decided here.
    return folder.add_file(
        name, record.logical_size, record.alloc_size, record.mtime, record.atime, _row_flags(record)
    )


def file_node(record, path):
    """The FileNode for a single-file scan target: exactly the row
    build_tree() attaches for `record` under its parent (a link to a file
    stays a link)."""
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
    or its parent turned out to be a link (never expanded) or an
    already-visited directory FRN (a cycle/duplicate-linked-directory
    guard -- see below). None of these raise; a caller that gets back a
    nonzero orphan_count can decide for itself whether that's tolerable or
    a reason to fall back to the Compatible engine.

    `row_frns` maps a folder Node to [(row index, FRN), ...] for each of
    its file rows whose record has more than one name (a hard link, which
    finalize_subtree() regroups) or is a link (which refuse_linked_folder()
    looks up) -- the side channel those need, since a row deliberately
    carries no FRN. Every other row's record has exactly one occurrence,
    so there's nothing to regroup.

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
    # The requested root is never flagged as a link -- matching scanner.py's
    # root Node. (A linked folder never gets here: see refuse_linked_folder.)
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
                if child_record.is_link or len(child_record.names) > 1:
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


class LinkedFolderError(RuntimeError):
    """The folder asked for is a junction, symbolic link or mount point,
    which Turbo Scan can't read (see refuse_linked_folder)."""

    def __init__(self, path):
        super().__init__(
            f"{path} is a junction, symbolic link or mount point; "
            "Turbo Scan reads only the folder's own MFT records, which for a link are empty"
        )


def refuse_linked_folder(node, target_path, records, row_frns):
    """Raise LinkedFolderError if `node` -- whatever find_subtree_node()
    located for `target_path` -- is a link to a folder, which build_tree()
    left as an unexpanded file row.

    Such a folder's own directory index is always empty (setting a
    junction on a non-empty folder fails with error 145), and its contents
    are records under the target, which may be on another volume. Turbo
    Scan used to rebuild the link's own record as the root and showed an
    empty tree where the Compatible engine found the target's files; the
    caller now falls back to the Compatible engine instead, which follows
    a linked scan root like any other (`os.path.isdir`).

    A link to a file is left alone: scanner.py doesn't follow that as a
    folder either.
    """
    if not node.is_link:
        return
    frn = _row_frn(row_frns, node)
    record = next((r for r in records if r.frn == frn), None) if frn is not None else None
    if record is not None and record.is_directory:
        raise LinkedFolderError(target_path)


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
