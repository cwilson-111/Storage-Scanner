"""The synthetic volume every benchmarks/scale.py scenario builds, and the
measurements the scenarios share. See scale.py for how they run and what's
gated."""

import os
import sqlite3
import sys

FILES_PER_DIR = 50
DIRS_PER_DIR = 8
VOLUME_ROOT = "C:\\" if os.name == "nt" else "/"
_MTIME = 1_750_000_000.0


# -- The synthetic volume ----------------------------------------------------- #


def layout(n_files):
    """[(folder path parts, files in it)], breadth-first, parents before
    children. Folder parts are relative to the volume root; () is the root,
    which holds no files."""
    n_dirs = max(1, -(-n_files // FILES_PER_DIR))
    folders = []
    frontier = [()]
    while len(folders) < n_dirs:
        next_frontier = []
        for parent in frontier:
            for i in range(DIRS_PER_DIR):
                if len(folders) == n_dirs:
                    break
                child = (*parent, f"dir_{i}")
                folders.append(child)
                next_frontier.append(child)
        frontier = next_frontier

    remaining = n_files
    result = [((), 0)]
    for parts in folders:
        count = min(FILES_PER_DIR, remaining)
        remaining -= count
        result.append((parts, count))
    return result


def _file_size(depth, index):
    # Deterministic, spread evenly over 0-5 MB (Knuth's multiplicative hash),
    # averaging ~2.5 MB so every folder crosses scan history's 50 MB
    # recording threshold, like the big folders people chart.
    return ((index + 1) * 2654435761 + depth * 40503) % 5_000_000


def small_subtree_parts(n_files):
    """A leaf folder: the "rescan one small folder" case."""
    return layout(n_files)[-1][0]


def build_node_tree(n_files):
    """The Node tree a Compatible scan of the synthetic volume would produce,
    with the same fields scanner.scan() sets, rolled up."""
    from storage_scanner.models import Node
    from storage_scanner.scanner import _rollup

    root = Node(VOLUME_ROOT, VOLUME_ROOT, True)
    nodes = {(): root}
    for parts, count in layout(n_files):
        if parts:
            parent = nodes[parts[:-1]]
            folder = Node(os.path.join(parent.path, parts[-1]), parts[-1], True)
            folder.mtime = folder.atime = _MTIME
            parent.children.append(folder)
            nodes[parts] = folder
        folder = nodes[parts]
        for index in range(count):
            name = f"file_{index:04d}.dat"
            child = Node(os.path.join(folder.path, name), name, False)
            child.size = child.alloc_size = _file_size(len(parts), index)
            child.mtime = child.atime = _MTIME + index
            child.file_count = 1
            folder.children.append(child)
    _rollup(root)
    return root


def synthetic_records(n_files):
    """The ParsedRecords a full Turbo Scan of the synthetic volume would
    produce: record 5 is the root, as on every NTFS volume."""
    from storage_scanner.mft_parser import FileNameAttr, ParsedRecord

    def frn(record_number):
        return (1 << 48) | record_number

    def record(record_number, is_dir, parent_frn, name, size):
        return ParsedRecord(
            frn=frn(record_number),
            is_directory=is_dir,
            file_attributes=0x10 if is_dir else 0x20,
            is_reparse_point=False,
            is_cloud_placeholder=False,
            mtime=_MTIME,
            atime=_MTIME,
            logical_size=size,
            alloc_size=size,
            names=[FileNameAttr(parent_frn=parent_frn, name=name, namespace=1)],
        )

    records = [record(5, True, frn(5), ".", 0)]
    folder_frns = {(): frn(5)}
    next_record = 64  # past NTFS's reserved system records
    for parts, count in layout(n_files):
        if parts:
            records.append(record(next_record, True, folder_frns[parts[:-1]], parts[-1], 0))
            folder_frns[parts] = frn(next_record)
            next_record += 1
        for index in range(count):
            size = _file_size(len(parts), index)
            records.append(
                record(next_record, False, folder_frns[parts], f"file_{index:04d}.dat", size)
            )
            next_record += 1
    return records


# -- Measurement helpers ------------------------------------------------------ #


def peak_rss_bytes():
    """This process's peak resident memory so far."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return counters.PeakWorkingSetSize

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


def database_bytes(path):
    """A SQLite file's size with its WAL folded in, as it would sit on disk
    after the app's next checkpoint."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return os.path.getsize(path)
