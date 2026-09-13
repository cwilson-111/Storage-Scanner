"""The scanned-tree data model."""


class Node:
    """A file or directory in the scanned tree."""
    __slots__ = (
        "path", "name", "is_dir", "size", "children", "file_count", "error",
        "is_link", "hardlink_dup",
    )

    def __init__(self, path, name, is_dir):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = 0            # total bytes (recursive for dirs)
        self.children = []       # list[Node]
        self.file_count = 0      # number of files contained (recursive)
        self.error = False       # True if we couldn't read this dir
        self.is_link = False     # symlink, junction, or other reparse point
        self.hardlink_dup = False  # extra hard link to content already counted
