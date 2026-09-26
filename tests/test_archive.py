import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import archive
from storage_scanner.models import Node, detached_file


def test_likely_compresses_well_true_for_text():
    assert archive.likely_compresses_well("/tmp/notes.txt") is True
    assert archive.likely_compresses_well("/tmp/data.csv") is True


def test_likely_compresses_well_false_for_already_compressed_formats():
    assert archive.likely_compresses_well("/tmp/movie.mp4") is False
    assert archive.likely_compresses_well("/tmp/photo.JPG") is False  # case-insensitive
    assert archive.likely_compresses_well("/tmp/archive.zip") is False


def test_unique_archive_path_avoids_collisions(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello")

    first = archive._unique_archive_path(str(target))
    assert first == str(target) + ".zip"

    # If that path is already taken, the next call should pick "(2)".
    Path(first).write_text("placeholder")
    second = archive._unique_archive_path(str(target))
    assert second == f"{target} (2).zip"


def test_archive_file_compresses_and_removes_original(tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("hello world" * 1000)

    node = detached_file(str(target), size=target.stat().st_size)

    monkeypatch.setattr(
        archive,
        "recycle_and_log",
        lambda node, source, action, extra_error_context=None: True,
    )

    result = archive.archive_file(node, source="Cleanup Recommendations")

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
    result = archive.archive_file(node, source="Cleanup Recommendations")
    assert result.success is False
    assert "files" in result.error.lower()


def test_archive_file_reports_partial_when_original_cannot_be_removed(tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("hello")
    node = detached_file(str(target), size=target.stat().st_size)

    monkeypatch.setattr(
        archive,
        "recycle_and_log",
        lambda node, source, action, extra_error_context=None: False,
    )

    result = archive.archive_file(node, source="Cleanup Recommendations")

    assert result.success is True  # the archive itself was created fine
    assert result.original_removed is False
    assert "both copies" in result.error
    assert result.archive_path in result.error  # ledger must still point at the .zip
    # The zip must still exist even though the original removal failed.
    assert os.path.exists(result.archive_path)


def test_archive_file_cleans_up_partial_archive_on_write_failure(tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_text("hello")
    node = detached_file(str(target), size=target.stat().st_size)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", boom)

    result = archive.archive_file(node, source="Cleanup Recommendations")

    assert result.success is False
    expected_zip = str(target) + ".zip"
    assert not os.path.exists(expected_zip), "partial/broken archive must not be left behind"
