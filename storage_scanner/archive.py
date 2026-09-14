"""Compress-and-remove archiving for Review-candidate files.

Compressing is safer than deleting: nothing is lost, just shrunk into a
.zip next to the original. The original is only ever removed — through the
same Recycle Bin/audit-log path every other deletion in the app uses —
after the archive has been written *and verified*, never before.
"""

import os
import zipfile
from collections import namedtuple

from storage_scanner.audit import recycle_and_log
from storage_scanner.logging_setup import logger

ArchiveResult = namedtuple(
    "ArchiveResult", ["success", "archive_path", "original_removed", "error"],
)

# Already-compressed or already-dense formats barely shrink further — this
# is a UI hint to set expectations, not a gate; archiving still works on
# any file type regardless.
_POOR_COMPRESSION_EXTENSIONS = {
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".tar",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".mkv", ".avi", ".webm",
    ".mp3", ".aac", ".flac", ".ogg", ".m4a",
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


def archive_file(node, source):
    """Compress `node` (a file) to a .zip beside it, verify the archive,
    then remove the original via recycle_and_log(). Never partially
    destructive: the original is untouched unless the archive was written
    and verified successfully first.
    """
    if node.is_dir:
        return ArchiveResult(False, None, False, "Archiving only supports individual files.")

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

    original_removed = recycle_and_log(node, source=source, action="archive")
    error = None if original_removed else (
        "Archived successfully, but the original could not be removed — both copies now exist."
    )
    return ArchiveResult(True, archive_path, original_removed, error)
