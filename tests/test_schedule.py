import os
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import schedule
from storage_scanner.cli import build_arg_parser
from storage_scanner.schedule import ScheduledScan

EXE = r"C:\Program Files\Storage Scanner\StorageScanner.exe"
FOLDER = r"C:\Users\me\My Documents"


def _scan(**overrides):
    fields = {"path": FOLDER, "frequency": "daily", "time": "09:00"}
    fields.update(overrides)
    return ScheduledScan(**fields)


def _command(scheduled):
    return schedule.scan_command(scheduled, launch_args=[EXE])


# --------------------------------------
# validation
# --------------------------------------


@pytest.mark.parametrize("time", ["9:00", "09:00", "23:59", "0:05"])
def test_valid_times_are_accepted(time):
    _scan(time=time).validate()


@pytest.mark.parametrize("time", ["24:00", "9", "09:60", "9am", ""])
def test_invalid_times_are_rejected(time):
    with pytest.raises(ValueError, match="HH:MM"):
        _scan(time=time).validate()


@pytest.mark.parametrize("path", ["", "relative/folder"])
def test_relative_or_empty_paths_are_rejected(path):
    with pytest.raises(ValueError, match="full folder path"):
        _scan(path=path).validate()


def test_unknown_frequency_and_weekday_are_rejected():
    with pytest.raises(ValueError, match="Frequency"):
        _scan(frequency="hourly").validate()

    with pytest.raises(ValueError, match="Day"):
        _scan(frequency="weekly", weekday="FUNDAY").validate()


# --------------------------------------
# the command a scheduler runs
# --------------------------------------


def test_scan_command_is_a_valid_history_saving_cli_invocation():
    command = _command(_scan())

    assert command[:2] == [EXE, "--cli"]
    args = build_arg_parser().parse_args(command[2:])
    assert args.path == FOLDER
    assert args.save_history is True
    assert args.format == "none"


def test_launch_args_use_the_executable_alone_when_frozen():
    assert schedule.app_launch_args(frozen=True, executable=EXE) == [EXE]


def test_launch_args_run_the_entry_script_from_source():
    args = schedule.app_launch_args(
        frozen=False,
        executable="/usr/bin/python3",
        script="/src/Storage-Scanner.py",
    )
    assert args[-1] == "/src/Storage-Scanner.py"


def test_launch_args_find_the_real_entry_script_from_source():
    args = schedule.app_launch_args(frozen=False, executable="/usr/bin/python3")

    assert os.path.basename(args[-1]) == "Storage-Scanner.py"
    assert os.path.exists(args[-1])


def test_task_name_is_stable_per_folder_and_safe_for_task_scheduler():
    name = schedule.task_name(_scan())

    assert name == schedule.task_name(_scan(frequency="weekly", time="18:30"))
    assert name == schedule.task_name(_scan(path=FOLDER + "\\"))
    assert name.startswith(f"{schedule.TASK_NAME_PREFIX} - My Documents (")
    assert not any(c in name.split(" - ", 1)[1] for c in '\\/:*?"<>|')


def test_folders_with_the_same_name_get_different_tasks():
    first = schedule.task_name(_scan(path=r"C:\Work\Reports"))
    second = schedule.task_name(_scan(path=r"D:\Archive\Reports"))

    assert first != second
    assert "Reports" in first and "Reports" in second


def test_a_drive_root_gets_a_usable_task_name():
    name = schedule.task_name(_scan(path="C:\\"))

    assert name.startswith(f"{schedule.TASK_NAME_PREFIX} - ")
    assert not any(c in name.split(" - ", 1)[1] for c in '\\/:*?"<>|')


# --------------------------------------
# Windows Task Scheduler
# --------------------------------------

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
TODAY = datetime(2026, 9, 23, 14, 30)


def _xml(scheduled, command=None):
    xml = schedule.windows_task_xml(
        scheduled,
        command=command or _command(scheduled),
        today=TODAY,
    )
    # ElementTree won't parse a str that declares an encoding; schtasks gets
    # the real UTF-16 bytes.
    return ET.fromstring(xml.encode("utf-16"))


def _text(root, path):
    return root.find(path, NS).text


def test_task_xml_runs_the_scan_command_with_arguments_kept_separate():
    root = _xml(_scan())

    assert _text(root, "t:Actions/t:Exec/t:Command") == EXE
    arguments = _text(root, "t:Actions/t:Exec/t:Arguments")
    assert arguments == subprocess.list2cmdline(_command(_scan())[1:])
    assert f'--cli "{FOLDER}"' in arguments


def test_daily_task_xml_starts_today_at_the_chosen_time():
    root = _xml(_scan(time="7:05"))
    trigger = "t:Triggers/t:CalendarTrigger"

    assert _text(root, f"{trigger}/t:StartBoundary") == "2026-09-23T07:05:00"
    assert _text(root, f"{trigger}/t:ScheduleByDay/t:DaysInterval") == "1"
    assert root.find(f"{trigger}/t:ScheduleByWeek", NS) is None


@pytest.mark.parametrize(
    "weekday, element", [("MON", "Monday"), ("FRI", "Friday"), ("SUN", "Sunday")]
)
def test_weekly_task_xml_names_the_day(weekday, element):
    root = _xml(_scan(frequency="weekly", weekday=weekday))
    days = root.find("t:Triggers/t:CalendarTrigger/t:ScheduleByWeek/t:DaysOfWeek", NS)

    assert [child.tag.split("}")[1] for child in days] == [element]


def test_task_xml_runs_as_the_user_without_elevation_and_catches_up_on_missed_runs():
    root = _xml(_scan())

    assert _text(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert _text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert _text(root, "t:Settings/t:StartWhenAvailable") == "true"
    assert _text(root, "t:Settings/t:DisallowStartIfOnBatteries") == "false"


def test_task_xml_escapes_characters_that_are_special_in_xml():
    folder = r"C:\R&D <archive>"
    root = _xml(_scan(path=folder), command=[EXE, "--cli", folder])

    assert folder in _text(root, "t:Actions/t:Exec/t:Arguments")
    assert folder in _text(root, "t:RegistrationInfo/t:Description")


def test_a_trailing_backslash_does_not_escape_the_closing_quote():
    # "D:\My Drive\" naively quoted would end in \" - an escaped quote - and
    # swallow every argument after it.
    command = [EXE, "--cli", "D:\\My Drive\\", "--save-history"]
    arguments = _text(_xml(_scan(), command=command), "t:Actions/t:Exec/t:Arguments")

    assert '"D:\\My Drive\\\\" --save-history' in arguments


def test_a_command_longer_than_the_old_261_character_limit_is_fine():
    long_folder = "C:\\" + "\\".join(["a long folder name"] * 20)
    scheduled = _scan(path=long_folder)

    arguments = _text(_xml(scheduled), "t:Actions/t:Exec/t:Arguments")

    assert len(subprocess.list2cmdline(_command(scheduled))) > 261
    assert long_folder in arguments


def test_create_and_delete_args_target_the_same_task_name():
    create = schedule.windows_create_args(_scan(), r"C:\Temp\task.xml")
    delete = schedule.windows_delete_args(_scan())

    assert create == [
        "schtasks",
        "/Create",
        "/TN",
        schedule.task_name(_scan()),
        "/XML",
        r"C:\Temp\task.xml",
        "/F",
    ]
    assert delete[delete.index("/TN") + 1] == schedule.task_name(_scan())


def test_create_windows_task_passes_a_utf16_xml_file_and_removes_it(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stdout = "SUCCESS: The scheduled task was successfully created."
        stderr = ""

    def fake_run(args, **_):
        xml_path = args[args.index("/XML") + 1]
        with open(xml_path, "rb") as f:
            seen["bytes"] = f.read()
        seen["path"] = xml_path
        return Result()

    monkeypatch.setattr(schedule.subprocess, "run", fake_run)
    monkeypatch.setattr(schedule, "app_launch_args", lambda: [EXE])

    ok, message = schedule.create_windows_task(_scan())

    assert ok is True
    assert "SUCCESS" in message
    assert seen["bytes"][:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 byte-order mark
    assert ET.fromstring(seen["bytes"]).tag.endswith("Task")
    assert not os.path.exists(seen["path"])


def test_create_windows_task_reports_schtasks_failure(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Access is denied."

    monkeypatch.setattr(schedule.subprocess, "run", lambda args, **_: Result())
    monkeypatch.setattr(schedule, "app_launch_args", lambda: [EXE])

    ok, message = schedule.create_windows_task(_scan())

    assert ok is False
    assert "Access is denied" in message


# --------------------------------------
# cron
# --------------------------------------


def test_daily_cron_line():
    scheduled = _scan(path="/home/me/data", time="18:30")
    line = schedule.cron_line(
        scheduled,
        command=["/opt/ss/StorageScanner", "--cli", "/home/me/data"],
    )

    assert line == "30 18 * * * /opt/ss/StorageScanner --cli /home/me/data"


@pytest.mark.parametrize("weekday, cron_day", [("MON", "1"), ("SAT", "6"), ("SUN", "0")])
def test_weekly_cron_line_uses_cron_day_numbers(weekday, cron_day):
    scheduled = _scan(path="/data", frequency="weekly", weekday=weekday)
    fields = schedule.cron_line(scheduled, command=["/app"]).split()

    assert fields[4] == cron_day


def test_cron_line_shell_quotes_paths_with_spaces():
    scheduled = _scan(path="/Users/me/My Files")
    command = [
        "/Applications/Storage Scanner.app/Contents/MacOS/StorageScanner",
        "--cli",
        "/Users/me/My Files",
    ]

    assert shlex.split(schedule.cron_line(scheduled, command=command))[5:] == command
