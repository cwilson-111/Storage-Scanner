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


def archive_file(node, source, remove_original):
    """Compress `node` (a file) to a .zip beside it, verify the archive,
    then remove the original with `remove_original(DeleteRequest)`, which
    returns a delete_service.DeleteResult (the app's delete service). Never
    partially destructive: the original is untouched unless the archive
    was written and verified successfully first.
    """
    if node.is_dir:
        return ArchiveResult(False, None, False, "Archiving only supports individual files.")
    # A path the original couldn't be deleted from isn't worth archiving --
    # and a Windows-trimmed name would zip a different file (delete_guard).
    refused = refusal_reason(node.path)
    if refused:
        return ArchiveResult(False, None, False, refused)

    archive_path = _unique_archive_path(node.path)

    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(node.path, arcname=node.name)
        with zipfile.ZipFile(archive_path) as zf:
            bad_member = zf.testzip()
        if bad_member is not None:
            raise zipfile.BadZipFile(f"verification failed for member {bad_member!r}")
    except (OSError, zipfile.BadZipFile) as exc:
        logger.exception("Archiving failed for %r", node.path)
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except OSError:
                logger.warning("Could not clean up partial archive %r", archive_path, exc_info=True)
        return ArchiveResult(False, None, False, str(exc))

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
