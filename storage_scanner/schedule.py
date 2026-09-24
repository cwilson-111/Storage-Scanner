"""Scheduled scans: builds the command a scheduler runs, and the Windows
Task Scheduler / cron entries that run it.

A scheduled scan is just the documented headless CLI
(`--cli <path> --save-history --format none`), so it lands in scan history
exactly like a scan run from the app — growth, forecasts, anomaly detection
and budgets all pick it up. Nothing here runs a scan itself.

On Windows the task is registered from a Task Scheduler XML definition
(`schtasks /Create /XML`) rather than `/TR "<command line>"`: /TR caps the
whole command at 261 characters, which a source checkout under a OneDrive
folder already exceeds before the scanned folder is even added, while the
XML keeps the program and its arguments in separate fields without that
limit. The XML also carries settings /TR can't express: run a missed scan as
soon as the PC is back on, and don't skip it on battery power.

Kept free of Tk so every command and XML document can be unit-tested. The
only side effects are create_windows_task()/delete_windows_task(), which
call schtasks.exe for the current user (no admin needed).
"""

import contextlib
import hashlib
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape

from storage_scanner.platform_support import IS_WINDOWS

FREQUENCIES = ("daily", "weekly")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
TASK_NAME_PREFIX = "Storage Scanner scan"

# Storage-Scanner.py, beside this package: what a source checkout runs.
ENTRY_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Storage-Scanner.py",
)

_WEEKDAY_ELEMENTS = {
    "MON": "Monday",
    "TUE": "Tuesday",
    "WED": "Wednesday",
    "THU": "Thursday",
    "FRI": "Friday",
    "SAT": "Saturday",
    "SUN": "Sunday",
}
# A scan that somehow hangs is stopped rather than left running forever.
TASK_TIME_LIMIT = "PT4H"

_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# Characters Task Scheduler won't accept in a task name (plus control chars).
_UNSAFE_TASK_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True)
class ScheduledScan:
    path: str
    frequency: str = "daily"  # "daily" | "weekly"
    time: str = "09:00"  # 24-hour HH:MM, local time
    weekday: str = "MON"  # weekly only

    def validate(self):
        """Raises ValueError with a message fit to show the user."""
        # posixpath too: on Windows, Python 3.13+ no longer calls "/data" absolute.
        if not self.path or not (os.path.isabs(self.path) or posixpath.isabs(self.path)):
            raise ValueError("Choose a full folder path to scan.")

        if self.frequency not in FREQUENCIES:
            raise ValueError(f"Frequency must be one of: {', '.join(FREQUENCIES)}.")

        if not _TIME_PATTERN.match(self.time):
            raise ValueError("Time must be 24-hour HH:MM, for example 09:00 or 18:30.")

        if self.frequency == "weekly" and self.weekday not in WEEKDAYS:
            raise ValueError(f"Day must be one of: {', '.join(WEEKDAYS)}.")

    @property
    def hour_minute(self):
        hour, minute = self.time.split(":")
        return int(hour), int(minute)


def app_launch_args(frozen=None, executable=None, script=None):
    """How to start this app from a scheduler: the packaged executable, or
    the interpreter plus Storage-Scanner.py when running from source. From
    source on Windows, pythonw.exe is preferred so a console window doesn't
    flash up on every scheduled run."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = executable or sys.executable

    if frozen:
        return [executable]

    # Found from this package's location, not sys.argv[0], which is "-c" or
    # "-m" (or "-") whenever the app wasn't started as a plain script.
    script = script or ENTRY_SCRIPT

    if IS_WINDOWS:
        pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")

        if os.path.basename(executable).lower() == "python.exe" and os.path.exists(pythonw):
            executable = pythonw

    return [executable, script]


def scan_command(scheduled, launch_args=None):
    """The full argv a scheduler runs for one scheduled scan."""
    launch_args = launch_args if launch_args is not None else app_launch_args()
    return [*launch_args, "--cli", scheduled.path, "--save-history", "--format", "none"]


def task_name(scheduled):
    """A stable Task Scheduler name per scanned folder, so scheduling the
    same folder again replaces its task instead of adding a second one:
    the folder's own name for reading, plus a short fingerprint of its full
    path so two folders that share a name get separate tasks."""
    normalized = os.path.normcase(os.path.normpath(scheduled.path))
    fingerprint = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:6]
    folder = os.path.basename(os.path.normpath(scheduled.path)) or scheduled.path
    label = _UNSAFE_TASK_NAME_CHARS.sub("_", folder).strip(" _.") or "drive"
    return f"{TASK_NAME_PREFIX} - {label} ({fingerprint})"


def windows_task_xml(scheduled, command=None, today=None):
    """Task Scheduler XML for one scheduled scan, run as the current user
    without elevation. The first run is `today` at the chosen time; if that
    has already passed, Task Scheduler simply waits for the next one."""
    scheduled.validate()
    command = command if command is not None else scan_command(scheduled)
    hour, minute = scheduled.hour_minute
    start = (today or datetime.now()).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if scheduled.frequency == "daily":
        schedule_xml = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    else:
        schedule_xml = (
            "<ScheduleByWeek><DaysOfWeek>"
            f"<{_WEEKDAY_ELEMENTS[scheduled.weekday]} />"
            "</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        )

    description = escape(f"Storage Scanner: scans {scheduled.path} and saves it to scan history.")

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.isoformat(timespec="seconds")}</StartBoundary>
      <Enabled>true</Enabled>
      {schedule_xml}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>{TASK_TIME_LIMIT}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command[0])}</Command>
      <Arguments>{escape(subprocess.list2cmdline(command[1:]))}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def windows_create_args(scheduled, xml_path):
    """schtasks.exe argv that creates (or, with /F, replaces) the task from
    the XML definition saved at xml_path."""
    return ["schtasks", "/Create", "/TN", task_name(scheduled), "/XML", xml_path, "/F"]


def windows_delete_args(scheduled):
    return ["schtasks", "/Delete", "/TN", task_name(scheduled), "/F"]


def cron_line(scheduled, command=None):
    """One crontab line for macOS/Linux (add it with `crontab -e`)."""
    scheduled.validate()
    command = command if command is not None else scan_command(scheduled)
    hour, minute = scheduled.hour_minute
    day_of_week = (
        "*"
        if scheduled.frequency == "daily"
        else str((WEEKDAYS.index(scheduled.weekday) + 1) % 7)  # cron: 0 = Sunday
    )
    return f"{minute} {hour} * * {day_of_week} {shlex.join(command)}"


def display_command(args):
    """A command line for showing or copying, quoted for this platform's shell."""
    return subprocess.list2cmdline(args) if IS_WINDOWS else shlex.join(args)


def _run_schtasks(args):
    """Runs schtasks.exe. Returns (ok, message)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    message = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, message


def create_windows_task(scheduled):
    """Registers (or replaces) the task. Returns (ok, message); raises
    ValueError if the schedule itself is invalid."""
    xml = windows_task_xml(scheduled)
    # schtasks reads the file itself, so it can't stay open (Windows locks it).
    handle, xml_path = tempfile.mkstemp(suffix=".xml", prefix="storage-scanner-task-")
    os.close(handle)

    try:
        # The document declares UTF-16, which is what schtasks expects.
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(xml)

        return _run_schtasks(windows_create_args(scheduled, xml_path))
    finally:
        with contextlib.suppress(OSError):
            os.remove(xml_path)


def delete_windows_task(scheduled):
    return _run_schtasks(windows_delete_args(scheduled))
