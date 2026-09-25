import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import schedule, scheduled_tasks
from storage_scanner.schedule import ScheduledScan
from storage_scanner.scheduled_tasks import (
    TASK_NOT_RUN_YET,
    definition_from_task_xml,
    parse_task_listing,
    split_windows_args,
)

EXE = r"C:\Program Files\Storage Scanner\StorageScanner.exe"
PYTHONW = r"C:\Python313\pythonw.exe"
SCRIPT = r"C:\Users\me\OneDrive\Storage Scanner\Storage-Scanner.py"
TODAY = datetime(2026, 9, 24, 14, 30)


def _task_xml(scheduled, launch_args=(EXE,), command=None):
    command = command or schedule.scan_command(scheduled, launch_args=list(launch_args))
    return schedule.windows_task_xml(scheduled, command=command, today=TODAY)


def _row(name, xml, last_run="2026-09-24T09:00:04", last_result=0, **extra):
    return {
        "name": name,
        "state": "Ready",
        "xml": xml,
        "lastRun": last_run,
        "lastResult": last_result,
        "nextRun": "2026-09-25T09:00:00",
        **extra,
    }


# --------------------------------------
# command-line splitting
# --------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--cli", r"C:\Users\me\My Documents", "--save-history"],
        ["--cli", "D:\\My Drive\\", "--notify"],  # trailing backslash before a quote
        ["--cli", r"C:\a\\b", 'say "hi"', 'ends with \\"'],
        ["", "--format", "none"],  # empty argument
        ["--cli", "C:\\Jérôme\\文档 & <x>\tand tab"],
        ["--cli", "\\\\server\\share\\folder"],  # UNC
    ],
)
def test_split_reverses_what_the_task_xml_writer_quoted(argv):
    assert split_windows_args(subprocess.list2cmdline(argv)) == argv


# --------------------------------------
# reading a task definition back
# --------------------------------------


@pytest.mark.parametrize(
    "scheduled",
    [
        ScheduledScan(path=r"C:\Users\me\My Documents", frequency="daily", time="07:05"),
        ScheduledScan(path="D:\\", frequency="weekly", time="18:30", weekday="SUN"),
        ScheduledScan(path=r"C:\R&D <archive>\\", frequency="weekly", time="00:00", weekday="WED"),
    ],
)
def test_a_saved_schedule_reads_back_as_the_same_schedule(scheduled):
    definition = definition_from_task_xml(_task_xml(scheduled))

    assert definition.scheduled == scheduled
    assert definition.notifies is True
    assert definition.launch == (EXE,)


def test_a_from_source_task_needs_both_the_interpreter_and_the_script():
    scheduled = ScheduledScan(path=r"C:\Data")

    definition = definition_from_task_xml(_task_xml(scheduled, launch_args=(PYTHONW, SCRIPT)))

    assert definition.launch == (PYTHONW, SCRIPT)


def test_a_task_saved_before_notifications_existed_is_flagged():
    scheduled = ScheduledScan(path=r"C:\Data")
    old_command = [EXE, "--cli", scheduled.path, "--save-history", "--format", "none"]

    task = parse_task_listing(json.dumps([_row("t", _task_xml(scheduled, command=old_command))]))[0]

    assert task.scheduled == scheduled
    assert task.problems(path_exists=lambda _p: True) == [
        "saved before over-budget notifications existed"
    ]


def test_a_task_edited_by_hand_is_listed_but_not_treated_as_a_scan():
    scheduled = ScheduledScan(path=r"C:\Data", frequency="weekly", weekday="MON")
    two_days = _task_xml(scheduled).replace("<Monday />", "<Monday /><Friday />")

    for xml in (two_days, "not xml at all", _task_xml(scheduled, command=[EXE, "--gui"])):
        task = parse_task_listing(json.dumps([_row("t", xml)]))[0]

        assert task.scheduled is None
        assert "not a scan this app created" in task.problems()[0]


def test_a_task_whose_app_has_moved_is_flagged():
    scheduled = ScheduledScan(path=r"C:\Data")
    xml = _task_xml(scheduled, launch_args=(PYTHONW, SCRIPT))
    task = parse_task_listing(json.dumps([_row("t", xml)]))[0]

    assert task.problems(path_exists=lambda p: p != SCRIPT) == [
        "the app has moved since this was saved"
    ]
    assert task.problems(path_exists=lambda _p: True) == []


def test_a_disabled_task_is_flagged():
    xml = _task_xml(ScheduledScan(path=r"C:\Data"))
    task = parse_task_listing(json.dumps([_row("t", xml, state="Disabled")]))[0]

    assert task.problems(path_exists=lambda _p: True) == ["disabled in Task Scheduler"]


# --------------------------------------
# last-run results and the listing
# --------------------------------------


@pytest.mark.parametrize(
    "last_run, last_result, text",
    [
        # Task Scheduler's "never ran" is a 1999 timestamp, not a null.
        ("1999-11-30T00:00:00", 0, "Hasn't run yet"),
        (None, TASK_NOT_RUN_YET, "Hasn't run yet"),
        ("2026-09-24T09:00:04", 0, "Succeeded"),
        ("2026-09-24T09:00:04", 1, "Failed (exit code 1)"),
        ("2026-09-24T09:00:04", 0x80070002, "Failed: program not found"),
        ("2026-09-24T09:00:04", 0x800710E0, "Failed (0x800710E0)"),
    ],
)
def test_last_result_is_described_in_plain_words(last_run, last_result, text):
    xml = _task_xml(ScheduledScan(path=r"C:\Data"))
    task = parse_task_listing(
        json.dumps([_row("t", xml, last_run=last_run, last_result=last_result)])
    )[0]

    assert task.last_result_text() == text


def test_listing_parses_times_and_sorts_by_name():
    xml = _task_xml(ScheduledScan(path=r"C:\Data"))
    rows = [_row("Storage Scanner scan - b", xml), _row("Storage Scanner scan - A", xml)]

    tasks = parse_task_listing(json.dumps(rows))

    assert [t.name for t in tasks] == ["Storage Scanner scan - A", "Storage Scanner scan - b"]
    assert tasks[0].last_run == datetime(2026, 9, 24, 9, 0, 4)
    assert tasks[0].next_run == datetime(2026, 9, 25, 9, 0)


def test_an_empty_listing_is_no_tasks():
    assert parse_task_listing("[]") == []


def test_list_windows_tasks_reports_powershell_failure(monkeypatch):
    class Result:
        returncode = 1
        stdout = b""
        stderr = b"Get-ScheduledTask : Access is denied."

    monkeypatch.setattr(scheduled_tasks.subprocess, "run", lambda *_a, **_k: Result())

    tasks, error = scheduled_tasks.list_windows_tasks()

    assert tasks is None
    assert "Access is denied" in error


def test_list_windows_tasks_reports_unreadable_output(monkeypatch):
    class Result:
        returncode = 0
        stdout = b"WARNING: something that isn't JSON"
        stderr = b""

    monkeypatch.setattr(scheduled_tasks.subprocess, "run", lambda *_a, **_k: Result())

    tasks, error = scheduled_tasks.list_windows_tasks()

    assert tasks is None
    assert "Unreadable" in error
