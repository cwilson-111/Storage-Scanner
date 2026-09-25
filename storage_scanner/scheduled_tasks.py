"""Reading this app's scheduled scans back out of Windows Task Scheduler,
for the list in the Schedule Scans window.

schtasks.exe can list tasks, but it prints in the console's 8-bit code page
(a folder named in, say, Cyrillic comes back as "????"), and its CSV headers
and dates are localized. PowerShell's ScheduledTasks module returns each
task's own XML definition -- the document schedule.create_windows_task()
registered -- plus ISO timestamps, as UTF-8 JSON. Everything but
list_windows_tasks() is pure, so it can be unit-tested on any platform.
"""

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from storage_scanner.schedule import TASK_NAME_PREFIX, WEEKDAY_ELEMENTS, ScheduledScan

TASK_XML_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
# Task Scheduler's LastTaskResult codes that aren't a program's exit code.
TASK_NOT_RUN_YET = 0x41303
TASK_RUNNING = 0x41301
_FILE_NOT_FOUND_RESULTS = (0x80070002, 0x80070003)

_DAY_ABBREVIATIONS = {element: day for day, element in WEEKDAY_ELEMENTS.items()}
_START_TIME = re.compile(r"T(\d{2}):(\d{2})")
_LIST_TIMEOUT_SECONDS = 60

_LIST_TASKS_SCRIPT = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$found = @(Get-ScheduledTask -TaskPath '\\' -TaskName '{TASK_NAME_PREFIX} - *' `
    -ErrorAction SilentlyContinue)
$rows = @(foreach ($t in $found) {{
    $info = Get-ScheduledTaskInfo -InputObject $t
    [pscustomobject]@{{
        name = $t.TaskName
        state = [string]$t.State
        xml = [string](Export-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath)
        lastRun = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('s') }} else {{ $null }}
        lastResult = $info.LastTaskResult
        nextRun = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('s') }} else {{ $null }}
    }}
}})
ConvertTo-Json -InputObject $rows -Depth 2 -Compress
"""


@dataclass(frozen=True)
class TaskDefinition:
    """What a task runs, read from its XML."""

    # None when the definition isn't a scan this app would have written
    # (edited by hand in Task Scheduler, say); it can still be removed.
    scheduled: Optional[ScheduledScan]
    # The program plus, from a source checkout, Storage-Scanner.py: what
    # has to still exist for the task to start.
    launch: tuple
    notifies: bool  # saved after --notify existed


@dataclass(frozen=True)
class RegisteredTask:
    """One "Storage Scanner scan - ..." task as Task Scheduler has it now."""

    name: str
    definition: TaskDefinition
    state: str  # Task Scheduler's own: Ready, Running, Disabled
    last_run: Optional[datetime]
    last_result: Optional[int]
    next_run: Optional[datetime]

    @property
    def scheduled(self):
        return self.definition.scheduled

    def last_result_text(self):
        if self.last_run is None or self.last_result in (None, TASK_NOT_RUN_YET):
            return "Hasn't run yet"
        if self.last_result == 0:
            return "Succeeded"
        if self.last_result == TASK_RUNNING:
            return "Running"
        if self.last_result in _FILE_NOT_FOUND_RESULTS:
            return "Failed: program not found"
        if self.last_result > 0xFFFF:
            return f"Failed (0x{self.last_result:08X})"
        return f"Failed (exit code {self.last_result})"

    def problems(self, path_exists=os.path.exists):
        """Reasons this task won't do what the Schedule window promises.
        All but the first are fixed by selecting it and saving it again."""
        if self.definition.scheduled is None:
            return ["not a scan this app created; remove it or fix it in Task Scheduler"]

        found = []
        if not all(path_exists(path) for path in self.definition.launch):
            found.append("the app has moved since this was saved")
        if not self.definition.notifies:
            found.append("saved before over-budget notifications existed")
        if self.state == "Disabled":
            found.append("disabled in Task Scheduler")
        return found


def split_windows_args(text):
    """Splits a Windows command line into argv the way the Microsoft C
    runtime (and so Python) does -- the inverse of subprocess.list2cmdline,
    which wrote the task's <Arguments>."""
    args = []
    current = []
    in_arg = False
    in_quotes = False
    i = 0

    while i < len(text):
        char = text[i]

        if char == "\\":
            start = i
            while i < len(text) and text[i] == "\\":
                i += 1
            count = i - start
            in_arg = True

            if i < len(text) and text[i] == '"':
                # 2n backslashes + quote: n backslashes, then the quote
                # toggles quoting; 2n+1: n backslashes and a literal quote.
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    i += 1
            else:
                current.append("\\" * count)
            continue

        if char == '"':
            in_quotes = not in_quotes
            in_arg = True
        elif char in " \t" and not in_quotes:
            if in_arg:
                args.append("".join(current))
                current = []
                in_arg = False
        else:
            current.append(char)
            in_arg = True
        i += 1

    if in_arg:
        args.append("".join(current))
    return args


def _schedule_from_trigger(trigger, path):
    if trigger is None:
        return None

    start = _START_TIME.search(trigger.findtext("t:StartBoundary", "", TASK_XML_NS))
    if start is None:
        return None
    time = start.expand(r"\1:\2")

    if trigger.find("t:ScheduleByDay", TASK_XML_NS) is not None:
        return ScheduledScan(path=path, frequency="daily", time=time)

    days = trigger.find("t:ScheduleByWeek/t:DaysOfWeek", TASK_XML_NS)
    if days is None or len(days) != 1:
        return None

    weekday = _DAY_ABBREVIATIONS.get(days[0].tag.rsplit("}", 1)[-1])
    if weekday is None:
        return None
    return ScheduledScan(path=path, frequency="weekly", time=time, weekday=weekday)


def definition_from_task_xml(xml):
    """TaskDefinition from a task's XML, as schedule.windows_task_xml()
    writes it and Export-ScheduledTask returns it."""
    try:
        # The declaration says UTF-16; the text arrives as a str.
        root = ET.fromstring(xml.encode("utf-16"))
    except ET.ParseError:
        return TaskDefinition(scheduled=None, launch=(), notifies=False)

    # Task Scheduler also accepts a quoted <Command>, as other apps write it.
    program = root.findtext("t:Actions/t:Exec/t:Command", "", TASK_XML_NS).strip().strip('"')
    argv = split_windows_args(root.findtext("t:Actions/t:Exec/t:Arguments", "", TASK_XML_NS))
    notifies = "--notify" in argv

    try:
        cli_index = argv.index("--cli")
        path = argv[cli_index + 1]
    except (ValueError, IndexError):
        return TaskDefinition(scheduled=None, launch=(program,), notifies=notifies)

    trigger = root.find("t:Triggers/t:CalendarTrigger", TASK_XML_NS)
    return TaskDefinition(
        scheduled=_schedule_from_trigger(trigger, path),
        launch=(program, *argv[:cli_index]),
        notifies=notifies,
    )


def _task_time(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Task Scheduler reports a task that never ran as having run in 1999.
    return None if moment.year < 2000 else moment


def parse_task_listing(text):
    """RegisteredTasks from _LIST_TASKS_SCRIPT's JSON output, by name."""
    tasks = [
        RegisteredTask(
            name=row["name"],
            definition=definition_from_task_xml(row.get("xml") or ""),
            state=row.get("state") or "",
            last_run=_task_time(row.get("lastRun")),
            last_result=row.get("lastResult"),
            next_run=_task_time(row.get("nextRun")),
        )
        for row in json.loads(text)
    ]
    return sorted(tasks, key=lambda task: task.name.lower())


def list_windows_tasks():
    """This app's scheduled scans. Returns (tasks, error_message); tasks is
    None on failure. Takes a few seconds (PowerShell loads the
    ScheduledTasks module), so call it off the UI thread."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _LIST_TASKS_SCRIPT],
            capture_output=True,
            timeout=_LIST_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)

    output = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode != 0 or not output:
        error = result.stderr.decode("utf-8", "replace").strip()
        return None, error or f"powershell.exe exited with code {result.returncode}"

    try:
        return parse_task_listing(output), ""
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"Unreadable task list from PowerShell: {exc}"
