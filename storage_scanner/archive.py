"""Compress-and-remove archiving for Review-candidate files.

Compressing is safer than deleting: nothing is lost, just shrunk into a
.zip next to the original. The original is only ever removed — through the
same delete service (storage_scanner.delete_service) every other deletion in
the app uses — after the archive has been written *and verified*, never
before.
"""

import errno
import os
import sys
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


def _create_new_archive_file(path):
    """`path` brought into existence and opened as a binary stream, or
    FileExistsError if anything is there already -- file, symbolic link,
    junction or extra hard link. Never creates or truncates a link's
    target.

    zipfile opens a path 'w+b', which follows a link and empties whatever
    it points at, and archiving can run elevated over a destination a
    same-user process at medium integrity is free to rename. So the
    archive has to be a file this call itself created: same hazard, same
    answer as mft_scan_cli.open_without_following_links.
    """
    if sys.platform != "win32":
        # O_EXCL refuses an existing symbolic link rather than following
        # it (even a dangling one), and a file it just created has exactly
        # one name, so no link-count check is needed here.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(fd, "wb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):  # BY_HANDLE_FILE_INFORMATION
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    # Its own instance, so these prototypes don't change the shared
    # ctypes.windll.kernel32 other modules call.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_write, create_new = 0x40000000, 1
    open_reparse_point = 0x00200000  # open a link itself, never its target
    reparse_attribute = 0x400
    already_there = (80, 183)  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS

    handle = kernel32.CreateFileW(
        str(path), generic_write, 0, None, create_new, open_reparse_point, None
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in already_there:
            # The caller's cue to try the next " (n).zip"; raised by hand
            # because ctypes.WinError's errno mapping isn't guaranteed.
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(path))
        raise ctypes.WinError(error)
    info = _FileInfo()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        raise error
    if info.attributes & reparse_attribute or info.links != 1:
        kernel32.CloseHandle(handle)
        raise OSError(f"Refusing to write {path}: it's a link to somewhere else")
    fd = msvcrt.open_osfhandle(handle, os.O_BINARY)  # type: ignore[attr-defined]
    return os.fdopen(fd, "wb")


def _create_unique_archive(path):
    """(`path` + ".zip", a binary stream to it), or "`path` (2).zip" etc.
    if that name is taken. Choosing the name and creating the file are one
    exclusive create, not os.path.exists() followed by an open: the gap
    between a check and a use is both a lost race with another archiver
    and the window to leave a link at the name we picked."""
    attempt = 1
    while True:
        candidate = f"{path}.zip" if attempt == 1 else f"{path} ({attempt}).zip"
        try:
            return candidate, _create_new_archive_file(candidate)
        except FileExistsError:
            attempt += 1


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
    archive_path, archive_file = _create_unique_archive(node.path)
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
            archive_file,
            zipfile.ZipFile(archive_file, "w", zipfile.ZIP_DEFLATED) as zf,
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
