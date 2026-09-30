"""Compress-and-remove archiving for Review-candidate files.

Compressing is safer than deleting: nothing is lost, just shrunk into a
.zip next to the original. The original is only ever removed — through the
same delete service (storage_scanner.delete_service) every other deletion in
the app uses — after the archive has been written *and verified*, never
before.
"""

import os
import zipfile
from collections import namedtuple

from storage_scanner.delete_guard import refusal_reason
from storage_scanner.delete_service import DeleteRequest
from storage_scanner.logging_setup import logger

ArchiveResult = namedtuple(
    "ArchiveResult",
    ["success", "archive_path", "original_removed", "error"],
)

# Already-compressed or already-dense formats barely shrink further — this
# is a UI hint to set expectations, not a gate; archiving still works on
# any file type regardless.
_POOR_COMPRESSION_EXTENSIONS = {
    ".zip",
    ".7z",
    ".rar",
    ".gz",
    ".bz2",
    ".xz",
    ".tar",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".mp3",
    ".aac",
    ".flac",
    ".ogg",
    ".m4a",
    ".pdf",
}


def likely_compresses_well(path):
    ext = os.path.splitext(path)[1].lower()
    return ext not in _POOR_COMPRESSION_EXTENSIONS


def _unique_archive_path(path):
    """`path` + ".zip", or "`path` (2).zip" etc. if that's already taken."""
    candidate = path + ".zip"
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = f"{path} ({n}).zip"
        if not os.path.exists(candidate):
            return candidate
        n += 1


# How much is read, compressed and reported at a time: small enough that
# progress moves and Cancel is noticed within a fraction of a second.
_CHUNK_BYTES = 1024 * 1024


class ArchiveCancelled(Exception):
    """write_verified_archive was cancelled; nothing was left behind."""


def write_verified_archive(node, cancel_event=None, progress=None):
    """Compress `node` (a file) to a .zip beside it and read it back to
    check every byte's CRC. Returns the archive's path. Touches no window
    and never the original, so it can run on a worker thread;
    `progress(done_bytes, total_bytes)` is called as it goes (writing,
    then verifying: total is twice the file's size). A failure raises
    OSError or zipfile.BadZipFile, a set `cancel_event` ArchiveCancelled;
    either way the partial archive is removed first."""
    archive_path = _unique_archive_path(node.path)
    total = 2 * max(node.size, 1)
    done = 0

    def step(count):
        nonlocal done
        if cancel_event is not None and cancel_event.is_set():
            raise ArchiveCancelled()
        done += count
        if progress is not None:
            progress(min(done, total), total)

    try:
        with (
            zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf,
            open(node.path, "rb") as source,
            zf.open(node.name, "w", force_zip64=True) as out,
        ):
            while chunk := source.read(_CHUNK_BYTES):
                out.write(chunk)
                step(len(chunk))
        # Reading a member to its end checks its CRC (BadZipFile if wrong).
        with zipfile.ZipFile(archive_path) as zf, zf.open(node.name) as member:
            while chunk := member.read(_CHUNK_BYTES):
                step(len(chunk))
    except BaseException:
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except OSError:
                logger.warning("Could not clean up partial archive %r", archive_path, exc_info=True)
        raise
    return archive_path


def check_archivable(node):
    """Why `node` can't be archived, or None."""
    if node.is_dir:
        return "Archiving only supports individual files."
    # A path the original couldn't be deleted from isn't worth archiving --
    # and a Windows-trimmed name would zip a different file (delete_guard).
    return refusal_reason(node.path)


def finish_archive(node, source, archive_path, remove_original):
    """After write_verified_archive: remove the original with
    `remove_original(DeleteRequest)`, which returns a delete_service.
    DeleteResult (the app's delete service), and say how it went."""
    result = remove_original(
        DeleteRequest(
            node,
            source,
            action="archive",
            error_context=f"archive already written to {archive_path}",
        )
    )
    error = (
        None
        if result.removed
        else (
            f"Archived to {archive_path}, but the original could not be removed "
            f"({result.message}) — both copies now exist."
        )
    )
    return ArchiveResult(True, archive_path, result.removed, error)


def archive_file(node, source, remove_original):
    """Compress `node` (a file) to a .zip beside it, verify the archive,
    then remove the original with `remove_original(DeleteRequest)`. Never
    partially destructive: the original is untouched unless the archive
    was written and verified successfully first. (The window runs the two
    halves, write_verified_archive and finish_archive, on different
    threads.)"""
    refused = check_archivable(node)
    if refused:
        return ArchiveResult(False, None, False, refused)
    try:
        archive_path = write_verified_archive(node)
    except (OSError, zipfile.BadZipFile) as exc:
        logger.exception("Archiving failed for %r", node.path)
        return ArchiveResult(False, None, False, str(exc))
    return finish_archive(node, source, archive_path, remove_original)
