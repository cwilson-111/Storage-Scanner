import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import archive
from storage_scanner.delete_outcome import FAILED, RECYCLED
from storage_scanner.delete_service import DeleteResult
from storage_scanner.models import Node, detached_file


def _removes_original(request):
    os.remove(request.node.path)
    return DeleteResult(request, RECYCLED)


def _cannot_remove_original(request):
    return DeleteResult(request, FAILED, "It may be in use.")


def _never_called(request):
    raise AssertionError("the original must not be touched")


def test_likely_compresses_well_true_for_text():
    assert archive.likely_compresses_well("/tmp/notes.txt") is True
    assert archive.likely_compresses_well("/tmp/data.csv") is True


def test_likely_compresses_well_false_for_already_compressed_formats():
    assert archive.likely_compresses_well("/tmp/movie.mp4") is False
    assert archive.likely_compresses_well("/tmp/photo.JPG") is False  # case-insensitive
    assert archive.likely_compresses_well("/tmp/archive.zip") is False


def test_the_archive_name_falls_back_to_two_when_zip_is_taken(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello")

    first, stream = archive._create_unique_archive(str(target))
    stream.close()
    assert first == str(target) + ".zip"

    # Picking the name created it, so the next call must step past it.
    second, stream = archive._create_unique_archive(str(target))
    stream.close()
    assert second == f"{target} (2).zip"

    third, stream = archive._create_unique_archive(str(target))
    stream.close()
    assert third == f"{target} (3).zip"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_an_archive_name_that_is_a_link_is_never_written_through(tmp_path):
    """P3-7 again: archiving can run elevated and the destination sits in
    a folder another process at the same user can write, so a name swapped
    for a link must not become a write to the link's target. A dangling
    link is the case os.path.exists() used to miss entirely."""
    target = tmp_path / "notes.txt"
    target.write_text("hello world" * 100)
    decoy = tmp_path / "decoy.txt"
    os.symlink(decoy, f"{target}.zip")  # the name archiving picks first

    node = detached_file(str(target), size=target.stat().st_size)
    archive_path = archive.write_verified_archive(node)

    assert not decoy.exists(), "the archive was written through the link"
    assert archive_path == f"{target} (2).zip"
    with zipfile.ZipFile(archive_path) as zf:
        assert zf.read("notes.txt").decode() == "hello world" * 100


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_a_link_left_at_the_chosen_name_cannot_truncate_its_target(tmp_path, monkeypatch):
    """The race the exclusive create closes: whatever another process does
    between picking a free name and opening it, the archive is a file this
    process brought into existence, never an existing one emptied."""
    target = tmp_path / "notes.txt"
    target.write_text("hello world" * 100)
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("keep me")
    chosen = f"{target}.zip"

    real_exists = os.path.exists

    def exists_then_lose_the_race(path):
        found = real_exists(path)
        if not found and str(path) == chosen and not os.path.islink(chosen):
            os.symlink(decoy, chosen)  # the attacker wins the window
        return found

    monkeypatch.setattr(os.path, "exists", exists_then_lose_the_race)

    node = detached_file(str(target), size=target.stat().st_size)
    archive_path = archive.write_verified_archive(node)

    assert decoy.read_text() == "keep me"
    assert zipfile.is_zipfile(archive_path)


def test_archive_file_compresses_and_removes_original(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello world" * 1000)

    node = detached_file(str(target), size=target.stat().st_size)

    result = archive.archive_file(node, "Cleanup Recommendations", _removes_original)

    assert result.success is True
    assert result.original_removed is True
    assert result.error is None
    assert os.path.exists(result.archive_path)
    assert result.archive_path.endswith(".zip")

    with zipfile.ZipFile(result.archive_path) as zf:
        assert zf.namelist() == ["notes.txt"]
        assert zf.read("notes.txt").decode() == "hello world" * 1000


def test_archive_file_rejects_directories():
    node = Node("/some/dir", "dir")
    result = archive.archive_file(node, "Cleanup Recommendations", _never_called)
    assert result.success is False
    assert "files" in result.error.lower()


def test_archive_file_reports_partial_when_original_cannot_be_removed(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello")
    node = detached_file(str(target), size=target.stat().st_size)

    result = archive.archive_file(node, "Cleanup Recommendations", _cannot_remove_original)

    assert result.success is True  # the archive itself was created fine
    assert result.original_removed is False
    assert "both copies" in result.error
    assert result.archive_path in result.error  # ledger must still point at the .zip
    # The zip must still exist even though the original removal failed.
    assert os.path.exists(result.archive_path)


def test_a_write_that_fails_part_way_leaves_no_archive_and_the_original(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello" * 500_000)  # a few chunks
    node = detached_file(str(target), size=target.stat().st_size)

    def disk_full(done, _total):
        if done > 0:
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        archive.write_verified_archive(node, progress=disk_full)

    assert not os.path.exists(str(target) + ".zip"), "partial archive must not be left behind"
    assert target.read_bytes() == b"hello" * 500_000


def test_cancelling_leaves_no_archive(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello" * 500_000)
    node = detached_file(str(target), size=target.stat().st_size)
    cancel = threading.Event()

    with pytest.raises(archive.ArchiveCancelled):
        archive.write_verified_archive(node, cancel, progress=lambda done, _total: cancel.set())

    assert not os.path.exists(str(target) + ".zip")
    assert target.exists()
