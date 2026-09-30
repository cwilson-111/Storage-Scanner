"""Finding duplicate files, and keeping duplicate groups true afterwards.

Tk-free: DuplicatesMixin (ui/duplicate_window.py) runs the finder and shows
its groups; delete_service re-checks a group on disk before deleting a copy
of it as a duplicate. A group is (size, (edge_digest, middle_digest), nodes):
the size the scan recorded, BLAKE2b of the first and last
DUPLICATE_HASH_CHUNK_BYTES, and of the middle chunk (None for a file no
larger than two chunks, which the first two already cover).
"""

import hashlib
import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from storage_scanner.cleanup_recommendations import is_protected_path, pick_keeper
from storage_scanner.delete_guard import name_is_trimmed_by_windows, windows_trims_path
from storage_scanner.models import FLAG_CLOUD_PLACEHOLDER, FileNode, iter_file_rows
from storage_scanner.platform_support import IS_WINDOWS
from storage_scanner.settings import DUPLICATE_HASH_CHUNK_BYTES

# -- Content sampling ------------------------------------------------------- #
# Both digests read at offsets derived from `size`: the size the scan
# recorded, which candidates are grouped on and is_sampled_duplicate()
# judges. A file that's no longer that size changed since the scan; its
# windows would no longer be the ones `size` implies, and a match could
# claim a byte-exact coverage it never had, so it hashes to None instead.


def partial_hash_file(path, size, cancel_event=None, chunk_size=DUPLICATE_HASH_CHUNK_BYTES):
    """BLAKE2b of the first and last `chunk_size` bytes of a file the scan
    recorded as `size` bytes.

    For a file no larger than 2 * chunk_size those two windows overlap or
    touch, so this digest already covers every byte. None if the file can't
    be read, is no longer `size` bytes, or the scan was cancelled.
    """
    if cancel_event and cancel_event.is_set():
        return None
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size != size:
                return None
            h = hashlib.blake2b(f.read(chunk_size), digest_size=32)
            if size > chunk_size:
                f.seek(size - chunk_size)
                h.update(f.read(chunk_size))
            return h.hexdigest()
    except OSError:
        return None


def middle_hash_file(path, size, cancel_event=None, chunk_size=DUPLICATE_HASH_CHUNK_BYTES):
    """BLAKE2b of the `chunk_size` bytes centered on the midpoint of a file
    the scan recorded as `size` bytes.

    The window starts at (size - chunk_size) // 2. For any file of
    2 * chunk_size < size <= 3 * chunk_size that start is <= chunk_size and
    its end is >= size - chunk_size, so together with the head and tail
    windows every byte is covered and a match is byte-exact. Above
    3 * chunk_size the bytes between the windows are never read: a match
    there is sampled, not verified. None if the file can't be read, is no
    longer `size` bytes, or the scan was cancelled.
    """
    if cancel_event and cancel_event.is_set():
        return None
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size != size:
                return None
            f.seek(max(0, (size - chunk_size) // 2))
            return hashlib.blake2b(f.read(chunk_size), digest_size=32).hexdigest()
    except OSError:
        return None


def sampled_digest(path, size):
    """The (edge, middle) digest the finder would give the file at `path`
    now, as a group key's second half; None if it can't be read or is no
    longer `size` bytes."""
    chunk_size = DUPLICATE_HASH_CHUNK_BYTES
    edge = partial_hash_file(path, size, chunk_size=chunk_size)
    if edge is None:
        return None
    if size <= 2 * chunk_size:
        return (edge, None)
    middle = middle_hash_file(path, size, chunk_size=chunk_size)
    return None if middle is None else (edge, middle)


def _collect_candidates(root_node, stats, progress_q, cancel_event, skip):
    """Every file under `root_node` worth hashing; None if cancelled."""
    all_files = []
    stack = [root_node] if root_node.is_dir else []
    # A name ending in a dot or space opens a different file on Windows (see
    # delete_guard), so its hash would be some other file's.
    trims = IS_WINDOWS
    if trims and windows_trims_path(root_node.path):
        stack = []

    while stack:
        if cancel_event.is_set():
            return None
        folder = stack.pop()

        if skip(folder.path) or (trims and name_is_trimmed_by_windows(folder.name)):
            for skipped_folder, rows in iter_file_rows(folder):
                sizes = skipped_folder.file_sizes
                for i in rows:
                    stats["files_skipped"] += 1
                    stats["bytes_skipped"] += sizes[i]
            if progress_q:
                progress_q.put(("stats", dict(stats)))
            continue

        stack.extend(folder.dirs)

        sizes, flags, names = folder.file_sizes, folder.file_flags, folder.file_names
        for i in folder.file_rows():
            size = sizes[i]
            if size <= 0:
                continue
            node = FileNode(folder, i)
            # Cloud placeholders (OneDrive Files On-Demand, etc.) report
            # their full logical size but aren't actually on local disk --
            # hashing one would force Windows to download it just to
            # compare it. Skip them entirely.
            if (
                flags[i] & FLAG_CLOUD_PLACEHOLDER
                or (trims and name_is_trimmed_by_windows(names[i]))
                or skip(node.path)
            ):
                stats["files_skipped"] += 1
                stats["bytes_skipped"] += size
                if progress_q:
                    progress_q.put(("stats", dict(stats)))
            else:
                all_files.append(node)
    return all_files


def _hash_all(jobs, job, progress_q, cancel_event, stat_key, stats, label, every):
    """Run `job(*args)` for every args tuple in `jobs` on a thread pool;
    the results in completion order, or None once cancelled."""
    total = max(1, len(jobs))
    max_workers = min(8, (os.cpu_count() or 4) * 2)
    completed = 0
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(job, *args) for args in jobs]
        for future in as_completed(futures):
            if cancel_event.is_set():
                return None
            results.append(future.result())
            completed += 1
            stats[stat_key] = completed
            if progress_q and (completed % every == 0 or completed == total):
                progress_q.put(("progress", completed, total, f"{label} … {completed:,}/{total:,}"))
    return results


def find_duplicate_files(root_node, progress_q=None, cancel_event=None, skip=is_protected_path):
    """Find duplicate files under `root_node`.

    1. Collect files (not protected, cloud-only, or Windows-trimmed names).
    2. Group by size.
    3. Hash the first and last chunk of files with matching sizes.
    4. Hash the middle chunk of files still matching, when they're larger
       than two chunks (smaller ones are already fully covered).

    Returns [(size, (edge_digest, middle_digest), nodes), ...], largest
    recoverable space first. Groups of files up to three chunks are
    byte-exact matches; larger ones only matched on the sampled windows
    (see cleanup_recommendations.is_sampled_duplicate).

    Progress goes only to `progress_q` ("stats" snapshots and "progress"
    ticks), never into caller state: two callers (the Duplicate Files
    window and Cleanup Recommendations) can run this at once.
    """
    if root_node is None:
        return []
    if cancel_event is None:
        cancel_event = threading.Event()

    stats = {
        "files_total": root_node.file_count,
        "files_checked": 0,
        "files_skipped": 0,
        "bytes_skipped": 0,
        "partial_hashed": 0,
        "middle_hashed": 0,
    }

    all_files = _collect_candidates(root_node, stats, progress_q, cancel_event, skip)
    if all_files is None:
        return []
    stats["files_checked"] = len(all_files)
    total_files = max(1, len(all_files))
    if progress_q:
        progress_q.put(("stats", dict(stats)))
        progress_q.put(("progress", 0, total_files, f"Collecting files … 0/{total_files:,}"))

    # Phase 1: only files with matching size can be duplicates.
    by_size = defaultdict(list)
    for index, node in enumerate(all_files, start=1):
        if cancel_event.is_set():
            return []
        by_size[node.size].append(node)
        if progress_q and (index % 1000 == 0 or index == total_files):
            progress_q.put(
                ("progress", index, total_files, f"Checking file sizes … {index:,}/{total_files:,}")
            )
    to_partial_hash = [(node,) for nodes in by_size.values() if len(nodes) > 1 for node in nodes]
    if not to_partial_hash:
        return []

    # Phase 2: hash first + last chunk.
    chunk_size = DUPLICATE_HASH_CHUNK_BYTES
    label = "Partial hashing possible duplicates"
    if progress_q:
        total = len(to_partial_hash)
        progress_q.put(("progress", 0, total, f"{label} … 0/{total:,}"))

    def partial_job(node):
        return node, partial_hash_file(node.path, node.size, cancel_event, chunk_size)

    partial = _hash_all(
        to_partial_hash, partial_job, progress_q, cancel_event, "partial_hashed", stats, label, 50
    )
    if partial is None:
        return []
    by_partial_hash = defaultdict(list)
    for node, digest in partial:
        if digest:
            by_partial_hash[(node.size, digest)].append(node)

    # Phase 3: hash the middle chunk of surviving candidates. Head + tail
    # already cover every byte of a file no larger than two chunks, so those
    # keep their phase-2 key as-is.
    by_final_key = defaultdict(list)
    to_middle_hash = []
    for (size, digest), nodes in by_partial_hash.items():
        if len(nodes) < 2:
            continue
        if size <= 2 * chunk_size:
            by_final_key[(size, (digest, None))].extend(nodes)
        else:
            to_middle_hash.extend((node, digest) for node in nodes)

    label = "Hashing middle of matching candidates"
    if to_middle_hash and progress_q:
        total = len(to_middle_hash)
        progress_q.put(("progress", 0, total, f"{label} … 0/{total:,}"))

    def middle_job(node, partial_digest):
        return (
            node,
            partial_digest,
            middle_hash_file(node.path, node.size, cancel_event, chunk_size),
        )

    middle = _hash_all(
        to_middle_hash, middle_job, progress_q, cancel_event, "middle_hashed", stats, label, 10
    )
    if middle is None:
        return []
    for node, partial_digest, middle_digest in middle:
        if middle_digest:
            by_final_key[(node.size, (partial_digest, middle_digest))].append(node)

    if progress_q:
        progress_q.put(("stats", dict(stats)))

    # Phase 4: the final duplicate list, most recoverable space first.
    duplicates = [
        (size, digest, nodes) for (size, digest), nodes in by_final_key.items() if len(nodes) > 1
    ]
    duplicates.sort(key=lambda item: item[0] * (len(item[2]) - 1), reverse=True)
    return duplicates


# -- Keeping groups true after deletes --------------------------------------- #


def find_group(groups, node):
    """The group `node` is a copy in, or None."""
    for group in groups or ():
        if node in group[2]:
            return group
    return None


def prune_groups(groups, deleted):
    """`groups` without the copies `deleted` (a delete_service.DeletedSet)
    covers. A group left with one copy isn't a duplicate of anything any
    more and is dropped entirely."""
    updated = []
    for size, digest, nodes in groups:
        remaining = [n for n in nodes if not deleted.covers(n)]
        if len(remaining) == len(nodes):
            updated.append((size, digest, nodes))
        elif len(remaining) > 1:
            updated.append((size, digest, remaining))
    return updated


def settle_group(nodes, keeper, deleted):
    """A shown group after a delete: (remaining copies, keeper). The keeper
    is None when fewer than two copies remain -- the group is no longer a
    duplicate group -- and is re-picked if the old one was deleted."""
    remaining = [n for n in nodes if not deleted.covers(n)]
    if len(remaining) < 2:
        return remaining, None
    if keeper not in remaining:
        keeper = pick_keeper(remaining)
    return remaining, keeper


def another_copy_exists(target, group):
    """True if some copy in `group` other than `target` is still on disk
    with the group's size and the same sampled windows the finder hashed."""
    size, digest, nodes = group
    return any(node != target and sampled_digest(node.path, size) == digest for node in nodes)
