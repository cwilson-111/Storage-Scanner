"""Tests for storage_scanner.mft_scan_cli's orchestration (arg parsing,
writing JSON output, exit codes) with open_record_source/
get_records_using_cache/build_tree/find_subtree_node mocked out -- those
are each already covered by their own dedicated test files
(test_mft_volume.py, test_mft_parser.py, test_turbo_cache.py,
test_usn_journal.py, test_mft_scan.py, test_turbo_scan.py). The real
end-to-end scan needs a real elevated Windows session, same as the rest of
Turbo Scan's raw-volume-reading path.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import mft_scan_cli
from storage_scanner.models import Node
from storage_scanner.turbo_scan import MftRead

_FULL_READ = MftRead(incremental=False, full_read_reason="first scan of this drive")


def _no_records(*_args, **_kwargs):
    return [], _FULL_READ


class _FakeRecordSource:
    def __init__(self, record_count):
        self.record_count = record_count
        self.closed = False

    def record_at(self, record_number):
        return b""  # parse_base_record is mocked in these tests

    def close(self):
        self.closed = True


def _make_node():
    node = Node("C:\\Data", "Data", True)
    node.size = 123
    node.file_count = 5
    return node


def _base_argv(output_path, subtree="C:\\Data"):
    return ["C:\\", "--subtree", subtree, "--output", str(output_path)]


def test_successful_scan_writes_json_output_and_closes_the_source(monkeypatch, tmp_path):
    fake_source = _FakeRecordSource(record_count=3)
    fake_node = _make_node()

    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)
    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", _no_records)
    monkeypatch.setattr(mft_scan_cli, "build_tree", lambda records, root_path: (fake_node, 0, {}))
    monkeypatch.setattr(mft_scan_cli, "find_subtree_node", lambda root, path: fake_node)

    output_path = tmp_path / "out.json"
    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path))

    assert exit_code == mft_scan_cli.EXIT_OK
    assert fake_source.closed is True

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["node"]["path"] == "C:\\Data"
    assert data["node"]["size"] == 123
    assert data["node"]["file_count"] == 5
    # How the MFT was read crosses the process boundary too, for the
    # unelevated GUI's scan-details strip.
    assert MftRead.from_dict(data["mft_read"]) == _FULL_READ


def test_source_is_closed_even_if_parsing_raises(monkeypatch, tmp_path):
    fake_source = _FakeRecordSource(record_count=3)
    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)

    def boom(*a, **k):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", boom)

    output_path = tmp_path / "out.json"
    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path))

    assert exit_code == mft_scan_cli.EXIT_SCAN_ERROR
    assert fake_source.closed is True
    assert not output_path.exists()


def test_missing_root_record_is_a_scan_error(monkeypatch, tmp_path, capsys):
    fake_source = _FakeRecordSource(record_count=1)
    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)
    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", _no_records)
    monkeypatch.setattr(mft_scan_cli, "build_tree", lambda records, root_path: (None, 0, {}))

    output_path = tmp_path / "out.json"
    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path))

    assert exit_code == mft_scan_cli.EXIT_SCAN_ERROR
    assert not output_path.exists()
    assert "root directory record" in capsys.readouterr().err


def test_subtree_not_found_is_a_scan_error(monkeypatch, tmp_path):
    fake_source = _FakeRecordSource(record_count=1)
    fake_node = _make_node()
    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)
    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", _no_records)
    monkeypatch.setattr(mft_scan_cli, "build_tree", lambda records, root_path: (fake_node, 0, {}))

    def missing(root, path):
        raise RuntimeError(f"Turbo Scan could not locate {path!r} in the volume tree")

    monkeypatch.setattr(mft_scan_cli, "find_subtree_node", missing)

    output_path = tmp_path / "out.json"
    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path, subtree="C:\\Nope"))

    assert exit_code == mft_scan_cli.EXIT_SCAN_ERROR
    assert not output_path.exists()


def test_unwritable_output_path_is_a_scan_error(monkeypatch, tmp_path):
    fake_source = _FakeRecordSource(record_count=1)
    fake_node = _make_node()
    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)
    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", _no_records)
    monkeypatch.setattr(mft_scan_cli, "build_tree", lambda records, root_path: (fake_node, 0, {}))
    monkeypatch.setattr(mft_scan_cli, "find_subtree_node", lambda root, path: fake_node)

    unwritable_dir = tmp_path / "does_not_exist"
    exit_code = mft_scan_cli.run_mft_scan(_base_argv(unwritable_dir / "out.json"))

    assert exit_code == mft_scan_cli.EXIT_SCAN_ERROR


def test_missing_required_arguments_exit_with_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        mft_scan_cli.run_mft_scan(["C:\\"])  # --subtree/--output both missing
    assert exc_info.value.code == 2


# -- --progress-file / _ProgressFileWriter ---------------------------------- #
#
# Before this existed, a Turbo Scan running through the elevated-helper
# path (run_elevated_scan_windows) posted zero progress of any kind for
# its entire duration -- often 15s-60s+ on a cold scan -- indistinguishable
# from a hang. Found via a real user report, not something this project's
# own earlier real-hardware validation had exercised (it always invoked
# this CLI directly from an already-elevated terminal, bypassing the
# actual ShellExecuteExW/UAC GUI flow that's the common case).


def test_progress_file_writer_writes_the_latest_progress_count(tmp_path):
    progress_path = tmp_path / "progress.txt"
    writer = mft_scan_cli._ProgressFileWriter(str(progress_path))

    writer.put(("progress", 100))
    assert progress_path.read_text(encoding="utf-8") == "100"

    writer.put(("progress", 5000))
    assert progress_path.read_text(encoding="utf-8") == "5000"  # overwritten, not appended


def test_progress_file_writer_ignores_non_progress_message_kinds(tmp_path):
    progress_path = tmp_path / "progress.txt"
    writer = mft_scan_cli._ProgressFileWriter(str(progress_path))

    writer.put(("root", object()))
    writer.put(("progress_bytes", 12345))

    assert not progress_path.exists()  # never touched -- only "progress" is relayed


def test_run_mft_scan_wires_progress_file_into_get_records_using_cache(monkeypatch, tmp_path):
    fake_source = _FakeRecordSource(record_count=3)
    fake_node = _make_node()
    captured = {}

    def fake_get_records(record_source, volume_root, progress_q, cancel_event):
        captured["progress_q"] = progress_q
        return [], _FULL_READ

    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)
    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", fake_get_records)
    monkeypatch.setattr(mft_scan_cli, "build_tree", lambda records, root_path: (fake_node, 0, {}))
    monkeypatch.setattr(mft_scan_cli, "find_subtree_node", lambda root, path: fake_node)

    output_path = tmp_path / "out.json"
    progress_path = tmp_path / "progress.txt"
    argv = _base_argv(output_path) + ["--progress-file", str(progress_path)]

    exit_code = mft_scan_cli.run_mft_scan(argv)

    assert exit_code == mft_scan_cli.EXIT_OK
    assert isinstance(captured["progress_q"], mft_scan_cli._ProgressFileWriter)
    assert captured["progress_q"].path == str(progress_path)


def test_run_mft_scan_without_progress_file_passes_none(monkeypatch, tmp_path):
    fake_source = _FakeRecordSource(record_count=3)
    fake_node = _make_node()
    captured = {}

    def fake_get_records(record_source, volume_root, progress_q, cancel_event):
        captured["progress_q"] = progress_q
        return [], _FULL_READ

    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: fake_source)
    monkeypatch.setattr(mft_scan_cli, "get_records_using_cache", fake_get_records)
    monkeypatch.setattr(mft_scan_cli, "build_tree", lambda records, root_path: (fake_node, 0, {}))
    monkeypatch.setattr(mft_scan_cli, "find_subtree_node", lambda root, path: fake_node)

    output_path = tmp_path / "out.json"
    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path))  # no --progress-file

    assert exit_code == mft_scan_cli.EXIT_OK
    assert captured["progress_q"] is None
