"""The scanned-tree data model.

A folder is a Node. The files directly inside it are not objects of their
own: each is one row of the folder's parallel columns (file_names,
file_sizes, file_allocs, file_mtimes, file_atimes, file_flags), and a
FileNode is a two-field view onto one row, made only when something asks
for it (node.children, node.files(), a search result, a duplicate group).
Both expose the same read-only attributes -- path, name, is_dir, size,
alloc_size, file_count, mtime, atime, error, is_link, hardlink_dup,
is_cloud_placeholder, children, has_children -- so code that only reads
the tree doesn't care which one it holds. Code that walks every file
(search, cleanup, duplicates, roll-up) reads the columns directly instead,
through iter_file_rows(), and makes a FileNode only for a row it keeps.

Why columns: a Python object per file (its slots, its own path and name
strings, an empty children list, boxed ints and floats) cost ~400 bytes a
file. A row costs its name string plus 33 bytes of packed numbers, and a
file's path isn't stored at all -- it's its folder's path joined with its
name. benchmarks/scale.py's tree_bytes_per_file gates the total.

Only a folder that holds files gets columns: until its first add_file()
they're shared empty sentinels (never appended to -- add_file() swaps in
real ones first), since six empty containers would cost over 400 bytes on
every folder that has only subfolders.

A deleted file's row is never physically removed: remove_child() flags it
FLAG_REMOVED and every iteration skips it. Row indexes never shift, so a
FileNode held anywhere -- in the cart, a search result, a duplicate group,
the main tree's row map -- always still means the same file.
"""

import os
from array import array

# Bits of Node.file_flags, one byte per file row.
FLAG_ERROR = 1  # its metadata couldn't be read
FLAG_LINK = 2  # symlink, junction, or other reparse point (never followed)
FLAG_HARDLINK_DUP = 4  # extra hard link to content already counted
FLAG_CLOUD_PLACEHOLDER = 8  # OneDrive/similar online-only file
FLAG_REMOVED = 16  # deleted from the tree after the scan

_SEPARATORS = "\\/" if os.name == "nt" else "/"

_NO_NAMES: "list[str]" = []
_NO_INTS: "array[int]" = array("Q")
_NO_FLOATS: "array[float]" = array("d")
_NO_FLAGS = bytearray()


def join_path(folder_path, name):
    """os.path.join(folder_path, name) for a name with no separators in it,
    without ntpath.join's drive parsing -- a file's path is rebuilt this way
    on every access."""
    if folder_path[-1:] in _SEPARATORS:  # a drive root like "C:\\" (or "")
        return folder_path + name
    return folder_path + os.sep + name


def row_flags(error=False, is_link=False, hardlink_dup=False, is_cloud_placeholder=False):
    """A file row's flags byte from its booleans."""
    return (
        (FLAG_ERROR if error else 0)
        | (FLAG_LINK if is_link else 0)
        | (FLAG_HARDLINK_DUP if hardlink_dup else 0)
        | (FLAG_CLOUD_PLACEHOLDER if is_cloud_placeholder else 0)
    )


class Node:
    """A folder in the scanned tree: its own totals (recursive once rolled
    up), its subfolders, and its files as rows of parallel columns."""

    __slots__ = (
        "path",
        "name",
        "size",
        "alloc_size",
        "file_count",
        "mtime",
        "atime",
        "error",
        "is_cloud_placeholder",
        "dirs",
        "file_names",
        "file_sizes",
        "file_allocs",
        "file_mtimes",
        "file_atimes",
        "file_flags",
        "removed_files",
    )

    is_dir = True
    # A reparse point is never followed below the scan root, so it's always
    # a file row; a followed root isn't flagged as a link. Folders can't be
    # hard-linked.
    is_link = False
    hardlink_dup = False

    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.size = 0  # total logical bytes (recursive, once rolled up)
        self.alloc_size = 0  # actual on-disk bytes (recursive, once rolled up)
        self.file_count = 0  # files contained (recursive, once rolled up)
        self.mtime = 0.0  # last-modified time, epoch seconds (0 if unknown)
        self.atime = 0.0  # last-accessed time, epoch seconds (0 if unknown)
        self.error = False  # couldn't be (fully) read; rolled up to ancestors
        self.is_cloud_placeholder = False  # online-only folder
        self.dirs = []  # list[Node]
        # One entry per file row. file_names is filled last on every
        # add_file(), so its length is the row count a reader can trust
        # while a scan is still appending (see scanner.scan's live tree).
        self.file_names = _NO_NAMES
        self.file_sizes = _NO_INTS  # logical bytes
        # On-disk bytes: differs from the logical size for sparse and
        # compressed files and cloud-placeholder stubs.
        self.file_allocs = _NO_INTS
        self.file_mtimes = _NO_FLOATS
        # Many filesystems update access times lazily or not at all, so
        # treat it as a weak "not touched" signal.
        self.file_atimes = _NO_FLOATS
        self.file_flags = _NO_FLAGS  # FLAG_* bits
        self.removed_files = 0  # rows flagged FLAG_REMOVED

    def __repr__(self):
        return f"<Node {self.path!r}>"

    def add_file(self, name, size=0, alloc_size=0, mtime=0.0, atime=0.0, flags=0):
        """Append a file row; returns its index."""
        names = self.file_names
        if names is _NO_NAMES:
            self.file_sizes = array("Q")
            self.file_allocs = array("Q")
            self.file_mtimes = array("d")
            self.file_atimes = array("d")
            self.file_flags = bytearray()
            names = self.file_names = []
        self.file_sizes.append(size)
        self.file_allocs.append(alloc_size)
        self.file_mtimes.append(mtime)
        self.file_atimes.append(atime)
        self.file_flags.append(flags)
        names.append(name)  # last -- see __init__
        return len(names) - 1

    def file_rows(self):
        """Indexes of this folder's files that are still in the tree."""
        count = len(self.file_names)
        if not self.removed_files:
            return range(count)
        flags = self.file_flags
        return [i for i in range(count) if not flags[i] & FLAG_REMOVED]

    def files(self):
        """This folder's files, as FileNode views."""
        return [FileNode(self, i) for i in self.file_rows()]

    @property
    def children(self):
        """Subfolders, then files -- a new list on every access."""
        return self.dirs + self.files()

    @property
    def has_children(self):
        return bool(self.dirs) or len(self.file_names) > self.removed_files

    def remove_child(self, child):
        """Take a deleted subfolder or file out of this folder. Totals are
        the caller's to adjust (see remove_from_tree). No-op if it isn't
        here (anymore)."""
        if child.is_dir:
            if child in self.dirs:
                self.dirs.remove(child)
        elif child.parent is self and not child.removed:
            self.file_flags[child.index] |= FLAG_REMOVED
            self.removed_files += 1


class FileNode:
    """One file row of a folder, read through the same attributes a Node
    has. Two views of the same row are equal and hash alike, so a file can
    be a dict key or set member however many times it's been looked up."""

    __slots__ = ("parent", "index")

    is_dir = False
    file_count = 1
    children = ()
    has_children = False

    def __init__(self, parent, index):
        self.parent = parent
        self.index = index

    def __eq__(self, other):
        return (
            isinstance(other, FileNode)
            and other.parent is self.parent
            and other.index == self.index
        )

    def __hash__(self):
        return hash((id(self.parent), self.index))

    def __repr__(self):
        return f"<FileNode {self.path!r}>"

    @property
    def name(self):
        return self.parent.file_names[self.index]

    @property
    def path(self):
        return join_path(self.parent.path, self.parent.file_names[self.index])

    @property
    def size(self):
        return self.parent.file_sizes[self.index]

    @property
    def alloc_size(self):
        return self.parent.file_allocs[self.index]

    @property
    def mtime(self):
        return self.parent.file_mtimes[self.index]

    @property
    def atime(self):
        return self.parent.file_atimes[self.index]

    @property
    def error(self):
        return bool(self.parent.file_flags[self.index] & FLAG_ERROR)

    @property
    def is_link(self):
        return bool(self.parent.file_flags[self.index] & FLAG_LINK)

    @property
    def hardlink_dup(self):
        return bool(self.parent.file_flags[self.index] & FLAG_HARDLINK_DUP)

    @property
    def is_cloud_placeholder(self):
        return bool(self.parent.file_flags[self.index] & FLAG_CLOUD_PLACEHOLDER)

    @property
    def removed(self):
        return bool(self.parent.file_flags[self.index] & FLAG_REMOVED)


def detached_file(path, size=0, alloc_size=0, mtime=0.0, atime=0.0, flags=0):
    """The FileNode for a scan target that is itself a file: the only row
    of a holder folder that belongs to no tree."""
    folder, name = os.path.split(path)
    holder = Node(folder, os.path.basename(folder) or folder)
    return FileNode(holder, holder.add_file(name, size, alloc_size, mtime, atime, flags))


def iter_folders(root):
    """Every folder in `root`'s tree, `root` first (none when it's a file).
    Iterative, so a very deep tree can't hit the recursion limit."""
    if not root.is_dir:
        return
    stack = [root]
    while stack:
        folder = stack.pop()
        yield folder
        stack.extend(folder.dirs)


def iter_file_rows(root):
    """(folder, row indexes) for every file in `root`'s tree -- or just
    `root`'s own row when it's a file."""
    if not root.is_dir:
        if not root.removed:
            yield root.parent, (root.index,)
        return
    for folder in iter_folders(root):
        yield folder, folder.file_rows()


def _folders_holding(root, node):
    """[root, ..., the folder directly holding `node`], or None when `node`
    isn't in `root`'s tree (already removed, `root` itself, or not a scanned
    node at all -- e.g. a cleanup_cache.CachedNode row from an earlier
    session)."""
    if not root.is_dir:
        return None
    if node.is_dir:

        def holds(folder):
            return node in folder.dirs

    else:
        if not isinstance(node, FileNode) or node.removed:
            return None

        def holds(folder):
            return folder is node.parent

    chain = []
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        del chain[depth:]
        chain.append(folder)
        if holds(folder):
            return chain
        stack.extend((child, depth + 1) for child in folder.dirs)
    return None


def remove_from_tree(root, node):
    """Unlink a deleted file or folder from `root`'s tree, taking its size
    and file count out of every folder above it. False if it wasn't there."""
    chain = _folders_holding(root, node)
    if chain is None:
        return False
    for folder in chain:
        folder.size -= node.size
        folder.file_count -= node.file_count
    chain[-1].remove_child(node)
    return True
