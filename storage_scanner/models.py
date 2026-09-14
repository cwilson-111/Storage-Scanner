"""The scanned-tree data model."""


class Node:
    """A file or directory in the scanned tree."""
    __slots__ = (
        "path", "name", "is_dir", "size", "children", "file_count", "error",
        "is_link", "hardlink_dup", "mtime", "alloc_size", "is_cloud_placeholder",
        "atime",
    )

    def __init__(self, path, name, is_dir):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = 0            # total bytes, logical (recursive for dirs)
        self.children = []       # list[Node]
        self.file_count = 0      # number of files contained (recursive)
        self.error = False       # True if we couldn't read this dir
        self.is_link = False     # symlink, junction, or other reparse point
        self.hardlink_dup = False  # extra hard link to content already counted
        self.mtime = 0.0         # last-modified time, epoch seconds (0 if unknown)
        self.alloc_size = 0      # actual on-disk bytes (recursive for dirs);
                                  # differs from `size` for sparse/compressed
                                  # files and cloud-placeholder stubs
        self.is_cloud_placeholder = False  # OneDrive/similar online-only file:
                                            # `size` is its full logical size,
                                            # but almost nothing is on local disk
        self.atime = 0.0         # last-accessed time, epoch seconds (0 if unknown);
                                  # many filesystems update this lazily or not at
                                  # all, so treat it as a weak "not touched" signal
