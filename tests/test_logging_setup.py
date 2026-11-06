"""logging_setup: the log file is rotated only when a process starts, and
never by renaming a file another process still has open (P3-5); and an
elevated process never appends its log through a link someone left in
%LOCALAPPDATA% (P3-7)."""

import logging
import os
import sys
import threading
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


@pytest.mark.windows
def test_the_log_is_never_appended_to_through_a_link(tmp_path):
    """The app can be elevated while its log folder is writable by the
    same user unelevated, and ProcessRedirectionTrustPolicy (the other
    half of this defence, redirection_guard) doesn't exist before Windows
    11 22H2 -- so the handle itself has to refuse a link."""
    protected = tmp_path / "protected.txt"
    protected.write_text("keep me")
    linked = tmp_path / logging_setup.LOG_FILE_NAME
    os.link(protected, linked)

    with pytest.raises(OSError, match="link"):
        logging_setup._append_stream(linked)
    assert protected.read_text() == "keep me"


def test_a_refused_log_file_falls_back_to_stderr_and_says_why(tmp_path, monkeypatch, caplog):
    """Refusing the log file must not take the app down with it, and the
    reason has to reach somewhere a bug report can show: a run with no log
    file otherwise looks exactly like a run that logged nothing."""

    def refuse(path):
        raise OSError(f"Refusing to write {path}: it's a link to somewhere else")

    monkeypatch.setattr(logging_setup, "_append_stream", refuse)
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV_VAR, str(tmp_path / "logs"))
    logger = logging.getLogger("storage_scanner")
    saved_handlers = logger.handlers[:]
    saved_hooks = (sys.excepthook, threading.excepthook)
    logger.handlers.clear()  # setup_logging returns early once it's configured
    try:
        with caplog.at_level(logging.WARNING, logger="storage_scanner"):
            assert logging_setup.setup_logging() is logger
        streams = [h for h in logger.handlers if type(h) is logging.StreamHandler]
        assert streams and all(h.stream is sys.stderr for h in streams)
    finally:
        logger.handlers[:] = saved_handlers
        sys.excepthook, threading.excepthook = saved_hooks

    assert any("stderr" in record.getMessage() for record in caplog.records), caplog.text
