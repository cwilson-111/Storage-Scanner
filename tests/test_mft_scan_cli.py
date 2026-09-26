"""Tests for storage_scanner.mft_scan_cli, the elevated Turbo Scan helper:
arguments, the JSON envelope it writes for the unelevated GUI, exit codes,
and the progress file. The scan itself (turbo_read.scan_subtree_using_cache)
is stubbed -- test_turbo_read.py, test_turbo_cache.py and
test_turbo_scan_integration.py cover it. A real run needs an elevated
Windows session, like the rest of Turbo Scan's raw-volume path.
"""

import json
import queue
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import mft_scan_cli
from storage_scanner.file_ops import _relay_progress_file
from storage_scanner.models import Node
from storage_scanner.scan_progress import Phase
from storage_scanner.turbo_read import MftRead

_FULL_READ = MftRead(incremental=False, full_read_reason="first scan of this drive")


class _FakeRecordSource:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _make_node():
    node = Node("C:\\Data", "Data", True)
    node.size = 123
    node.file_count = 5
    return node


def _base_argv(output_path, subtree="C:\\Data"):
    return ["C:\\", "--subtree", subtree, "--output", str(output_path)]


def _stub_scan(monkeypatch, scan):
    source = _FakeRecordSource()
    monkeypatch.setattr(mft_scan_cli, "open_record_source", lambda drive: source)
    monkeypatch.setattr(mft_scan_cli, "scan_subtree_using_cache", scan)
    return source


def test_successful_scan_writes_the_envelope_and_closes_the_source(monkeypatch, tmp_path):
    calls = []

    def scan(record_source, volume_root, target_path, progress_q, cancel_event):
        calls.append((volume_root, target_path))
        return _make_node(), _FULL_READ

    source = _stub_scan(monkeypatch, scan)
    output_path = tmp_path / "out.json"

    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path))

    assert exit_code == mft_scan_cli.EXIT_OK
    assert source.closed is True
    assert calls == [("C:\\", "C:\\Data")]
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert (data["node"]["path"], data["node"]["size"], data["node"]["file_count"]) == (
        "C:\\Data",
        123,
        5,
    )
    # How the MFT was read crosses the process boundary too, for the
    # unelevated GUI's scan-details strip.
    assert MftRead.from_dict(data["mft_read"]) == _FULL_READ


def test_a_failed_scan_exits_with_an_error_writes_nothing_and_closes_the_source(
    monkeypatch, tmp_path, capsys
):
    def scan(*_args, **_kwargs):
        raise RuntimeError("Turbo Scan could not locate 'C:\\\\Nope' in the volume tree")

    source = _stub_scan(monkeypatch, scan)
    output_path = tmp_path / "out.json"

    exit_code = mft_scan_cli.run_mft_scan(_base_argv(output_path, subtree="C:\\Nope"))

    assert exit_code == mft_scan_cli.EXIT_SCAN_ERROR
    assert source.closed is True
    assert not output_path.exists()
    assert "could not locate" in capsys.readouterr().err


def test_unwritable_output_path_is_a_scan_error(monkeypatch, tmp_path):
    _stub_scan(monkeypatch, lambda *a, **k: (_make_node(), _FULL_READ))

    exit_code = mft_scan_cli.run_mft_scan(_base_argv(tmp_path / "does_not_exist" / "out.json"))

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


def test_a_phase_written_by_the_helper_reaches_the_gui_queue_unchanged_and_once(tmp_path):
    progress_path = tmp_path / "progress.txt"
    writer = mft_scan_cli._ProgressFileWriter(str(progress_path))
    progress_q = queue.Queue()

    reading = Phase("Reading the MFT", 4096, 250_000, "records")
    writer.put(("phase", reading))
    last = _relay_progress_file(str(progress_path), progress_q, None)
    last = _relay_progress_file(str(progress_path), progress_q, last)  # nothing new
    saving = Phase("Saving the Turbo Scan cache")
    writer.put(("phase", saving))  # replaces, not appends
    _relay_progress_file(str(progress_path), progress_q, last)

    posted = []
    while not progress_q.empty():
        posted.append(progress_q.get_nowait())
    assert posted == [("phase", reading), ("phase", saving)]


def test_progress_file_writer_ignores_other_message_kinds(tmp_path):
    progress_path = tmp_path / "progress.txt"
    writer = mft_scan_cli._ProgressFileWriter(str(progress_path))

    writer.put(("root", object()))
    writer.put(("walk", object()))

    assert not progress_path.exists()  # never touched -- only "phase" is relayed


def test_run_mft_scan_relays_progress_through_the_progress_file(monkeypatch, tmp_path):
    captured = {}

    def scan(record_source, volume_root, target_path, progress_q, cancel_event):
        captured["progress_q"] = progress_q
        return _make_node(), _FULL_READ

    _stub_scan(monkeypatch, scan)
    progress_path = tmp_path / "progress.txt"
    argv = _base_argv(tmp_path / "out.json") + ["--progress-file", str(progress_path)]

    assert mft_scan_cli.run_mft_scan(argv) == mft_scan_cli.EXIT_OK
    assert isinstance(captured["progress_q"], mft_scan_cli._ProgressFileWriter)
    assert captured["progress_q"].path == str(progress_path)


def test_run_mft_scan_without_progress_file_passes_none(monkeypatch, tmp_path):
    captured = {}

    def scan(record_source, volume_root, target_path, progress_q, cancel_event):
        captured["progress_q"] = progress_q
        return _make_node(), _FULL_READ

    _stub_scan(monkeypatch, scan)

    assert mft_scan_cli.run_mft_scan(_base_argv(tmp_path / "out.json")) == mft_scan_cli.EXIT_OK
    assert captured["progress_q"] is None
