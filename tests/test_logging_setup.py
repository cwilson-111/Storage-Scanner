"""logging_setup: the log file is rotated only when a process starts, and
never by renaming a file another process still has open (P3-5)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import logging_setup


def _log(tmp_path, size):
    path = tmp_path / logging_setup.LOG_FILE_NAME
    path.write_bytes(b"x" * size)
    return path


def test_a_small_log_is_kept_as_it_is(tmp_path):
    path = _log(tmp_path, 10)

    logging_setup._rotate_at_start(path)

    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_a_full_log_moves_to_backup_one_and_older_backups_shift(tmp_path):
    path = _log(tmp_path, logging_setup._MAX_BYTES)
    (tmp_path / f"{path.name}.1").write_text("older")
    (tmp_path / f"{path.name}.3").write_text("oldest, dropped")

    logging_setup._rotate_at_start(path)

    assert not path.exists()
    assert (tmp_path / f"{path.name}.1").stat().st_size == logging_setup._MAX_BYTES
    assert (tmp_path / f"{path.name}.2").read_text() == "older"
    assert (tmp_path / f"{path.name}.3").read_text() == "oldest, dropped"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename an open file")
def test_a_full_log_another_process_has_open_is_left_to_append_to(tmp_path):
    path = _log(tmp_path, logging_setup._MAX_BYTES)

    with open(path, "ab"):
        logging_setup._rotate_at_start(path)

    assert path.stat().st_size == logging_setup._MAX_BYTES


def test_appends_from_two_streams_both_land(tmp_path):
    """What two processes do: each writes at the end, never over the
    other's lines."""
    path = tmp_path / logging_setup.LOG_FILE_NAME
    first, second = logging_setup._append_stream(path), logging_setup._append_stream(path)
    for index in range(50):
        for stream, name in ((first, "a"), (second, "b")):
            stream.write(f"{name}{index}\n")
            stream.flush()
    first.close()
    second.close()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 100
