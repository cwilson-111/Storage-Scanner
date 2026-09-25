"""Export Results and Schedule Scans: getting scan data out of the app, and
running scans without it.

A mixin composed into StorageScannerApp (storage_scanner/app.py). The file
shapes come from storage_scanner/export.py (the same writers the headless
`--cli` uses), and every scheduler command comes from
storage_scanner/schedule.py — this file is only the windows around them.
"""

import os
import queue
import threading
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    StringVar,
    Text,
    Toplevel,
    W,
    X,
    filedialog,
    messagebox,
    ttk,
)

from storage_scanner import schedule
from storage_scanner.export import export_to_file
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_WINDOWS, resource_path
from storage_scanner.scheduled_tasks import list_windows_tasks
from storage_scanner.settings import COLORS

_EXPORT_FILE_TYPES = {
    "csv": [("CSV (one row per file and folder)", "*.csv")],
    "json": [("JSON (nested folder tree)", "*.json")],
}


def _when_text(scheduled):
    day = "day" if scheduled.frequency == "daily" else scheduled.weekday.title()
    return f"Every {day} at {scheduled.time}"


def _moment_text(moment):
    return moment.strftime("%Y-%m-%d %H:%M") if moment is not None else "—"


class AutomationMixin:
    # -- Export Results ---------------------------------------------------- #

    def export_results(self):
        node = self.root_node

        if node is None:
            messagebox.showinfo("Export Results", "Run a scan first.")
            return

        base = os.path.basename(os.path.normpath(node.path)).strip(":\\/") or "scan"
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Results",
            initialfile=f"{base}-scan.csv",
            defaultextension=".csv",
            filetypes=_EXPORT_FILE_TYPES["csv"] + _EXPORT_FILE_TYPES["json"],
        )

        if not filename:
            return

        fmt = "json" if filename.lower().endswith(".json") else "csv"

        try:
            export_to_file(node, filename, fmt)
        except OSError as exc:
            logger.warning("Export to %r failed", filename, exc_info=True)
            messagebox.showerror("Export Results", f"Could not write the file:\n{exc}")
            return

        self.status_var.set(
            f"Exported {node.path} ({human_size(node.size)}, {node.file_count:,} files) "
            f"to {filename}"
        )

    # -- Schedule Scans ---------------------------------------------------- #

    def show_schedule_scans(self):
        existing = getattr(self, "_schedule_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._schedule_win = win
        win.configure(bg=COLORS["bg"])
        win.title("Schedule Scans")
        win.geometry("860x700" if IS_WINDOWS else "760x470")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Schedule Scans window iconbitmap failed", exc_info=True)

        ttk.Label(
            win,
            padding=(10, 8),
            style="Accent.TLabel",
            text="Scan a folder automatically and keep its growth history up to date",
        ).pack(side=TOP, fill=X)

        scheduler = "Windows Task Scheduler" if IS_WINDOWS else "cron"
        ttk.Label(
            win,
            padding=(10, 0, 10, 8),
            foreground=COLORS["muted"],
            wraplength=720,
            justify=LEFT,
            text=(
                f"Runs a headless scan through {scheduler} and saves it to scan history, "
                "exactly like a scan from this window, so Growth History, forecasts, "
                "anomalies and budgets include it. If the folder is over its budget, "
                "you get a desktop notification. It runs as you, without admin rights, "
                "so folders only an administrator can read are skipped. The app does "
                "not need to be open. Schedules saved before notifications existed "
                "need to be saved again to get them."
            ),
        ).pack(side=TOP, fill=X)

        form = ttk.Frame(win, padding=(10, 0, 10, 6))
        form.pack(side=TOP, fill=X)

        current = self.root_node.path if self.root_node is not None else self.path_var.get()
        path_var = StringVar(value=current or "")
        frequency_var = StringVar(value="daily")
        weekday_var = StringVar(value="MON")
        time_var = StringVar(value="09:00")

        ttk.Label(form, text="Folder").grid(row=0, column=0, sticky=W, pady=3)
        ttk.Entry(form, textvariable=path_var).grid(
            row=0, column=1, columnspan=4, sticky="ew", padx=6
        )

        def browse():
            chosen = filedialog.askdirectory(parent=win, initialdir=path_var.get() or None)
            if chosen:
                path_var.set(os.path.normpath(chosen))

        ttk.Button(form, text="Browse…", command=browse).grid(row=0, column=5)

        ttk.Label(form, text="Every").grid(row=1, column=0, sticky=W, pady=3)
        ttk.Combobox(
            form,
            textvariable=frequency_var,
            values=schedule.FREQUENCIES,
            state="readonly",
            width=8,
        ).grid(row=1, column=1, sticky=W, padx=6)
        ttk.Label(form, text="on").grid(row=1, column=2, sticky=W)
        weekday_combo = ttk.Combobox(
            form,
            textvariable=weekday_var,
            values=schedule.WEEKDAYS,
            state="readonly",
            width=6,
        )
        weekday_combo.grid(row=1, column=3, sticky=W, padx=6)
        ttk.Label(form, text="at (HH:MM, 24-hour)").grid(row=2, column=0, sticky=W, pady=3)
        ttk.Entry(form, textvariable=time_var, width=8).grid(row=2, column=1, sticky=W, padx=6)
        form.columnconfigure(4, weight=1)

        ttk.Label(
            win,
            padding=(10, 6, 10, 2),
            text="Scheduled task" if IS_WINDOWS else "Crontab line (add it with `crontab -e`)",
        ).pack(side=TOP, fill=X)

        preview = Text(
            win,
            height=5,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["fg"],
            relief="flat",
            padx=8,
            pady=6,
        )
        preview.pack(side=TOP, fill=X if IS_WINDOWS else BOTH, expand=not IS_WINDOWS, padx=10)

        status_var = StringVar()
        ttk.Label(win, textvariable=status_var, padding=(10, 4), wraplength=720, justify=LEFT).pack(
            side=TOP,
            fill=X,
        )

        def current_schedule():
            return schedule.ScheduledScan(
                path=path_var.get().strip(),
                frequency=frequency_var.get(),
                time=time_var.get().strip(),
                weekday=weekday_var.get(),
            )

        def entry_text(scheduled):
            if not IS_WINDOWS:
                return schedule.cron_line(scheduled)

            scheduled.validate()
            return (
                f"Task: {schedule.task_name(scheduled)}\n"
                f"When: {_when_text(scheduled)}, or as soon as the PC is back on "
                "if that time was missed\n"
                f"Runs: {schedule.display_command(schedule.scan_command(scheduled))}"
            )

        def command_text(scheduled):
            if not IS_WINDOWS:
                return schedule.cron_line(scheduled)

            scheduled.validate()
            return schedule.display_command(schedule.scan_command(scheduled))

        def refresh(*_):
            weekday_combo.config(
                state="readonly" if frequency_var.get() == "weekly" else "disabled"
            )
            preview.config(state="normal")
            preview.delete("1.0", END)

            try:
                preview.insert("1.0", entry_text(current_schedule()))
                status_var.set("")
            except ValueError as exc:
                status_var.set(str(exc))

            preview.config(state="disabled")

        for var in (path_var, frequency_var, weekday_var, time_var):
            var.trace_add("write", refresh)

        def copy_entry():
            try:
                text = command_text(current_schedule())
            except ValueError as exc:
                messagebox.showerror("Schedule Scans", str(exc), parent=win)
                return

            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            status_var.set("Copied to the clipboard.")

        def create_task():
            scheduled = current_schedule()

            try:
                ok, message = schedule.create_windows_task(scheduled)
            except ValueError as exc:
                messagebox.showerror("Schedule Scans", str(exc), parent=win)
                return

            if ok:
                status_var.set(
                    f'Scheduled "{schedule.task_name(scheduled)}". It appears in Task '
                    "Scheduler under that name; scheduling this folder again replaces it."
                )
                load_tasks()
            else:
                logger.warning("schtasks /Create failed: %s", message)
                messagebox.showerror(
                    "Schedule Scans",
                    f"Task Scheduler refused the task:\n{message}",
                    parent=win,
                )

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        if not IS_WINDOWS:
            ttk.Button(
                button_bar,
                text="Copy Crontab Line",
                style="Primary.TButton",
                command=copy_entry,
            ).pack(side=LEFT)
            refresh()
            return

        ttk.Button(
            button_bar,
            text="Create Scheduled Task",
            style="Primary.TButton",
            command=create_task,
        ).pack(side=LEFT)
        ttk.Button(button_bar, text="Copy Command", command=copy_entry).pack(side=RIGHT)

        # -- Scans already scheduled on this PC -- #

        list_header = ttk.Frame(win, padding=(10, 6, 10, 2))
        list_header.pack(side=TOP, fill=X)
        list_status_var = StringVar()
        ttk.Label(list_header, text="Scheduled scans on this PC").pack(side=LEFT)
        ttk.Label(list_header, textvariable=list_status_var, foreground=COLORS["muted"]).pack(
            side=LEFT,
            padx=8,
        )

        list_frame = ttk.Frame(win, padding=(10, 0, 10, 4))
        list_frame.pack(side=TOP, fill=BOTH, expand=True)
        cols = ("folder", "when", "last", "result", "next", "notes")
        tv = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse", height=5)
        for col, heading, width, stretch in (
            ("folder", "Folder", 190, True),
            ("when", "When", 130, False),
            ("last", "Last run", 115, False),
            ("result", "Result", 140, False),
            ("next", "Next run", 115, False),
            ("notes", "Needs attention", 200, True),
        ):
            tv.heading(col, text=heading)
            tv.column(col, width=width, anchor=W, stretch=stretch)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        tv.tag_configure("attention", foreground=COLORS["warning"])

        iid_to_task = {}
        results = queue.Queue()

        def show_tasks(tasks, error):
            tv.delete(*tv.get_children())
            iid_to_task.clear()

            if tasks is None:
                logger.warning("Listing scheduled scans failed: %s", error)
                list_status_var.set(f"Couldn't read Task Scheduler: {error}")
                return

            list_status_var.set(
                f"{len(tasks)} scheduled — select one to change it" if tasks else "None yet"
            )
            for index, task in enumerate(tasks):
                problems = task.problems()
                scheduled = task.scheduled
                iid = tv.insert(
                    "",
                    END,
                    values=(
                        scheduled.path if scheduled else task.name,
                        _when_text(scheduled) if scheduled else "—",
                        _moment_text(task.last_run),
                        task.last_result_text(),
                        _moment_text(task.next_run),
                        "; ".join(problems),
                    ),
                    tags=("odd" if index % 2 else "even", *(("attention",) if problems else ())),
                )
                iid_to_task[iid] = task

        def poll_tasks():
            if not win.winfo_exists():
                return
            try:
                tasks, error = results.get_nowait()
            except queue.Empty:
                win.after(150, poll_tasks)
                return
            show_tasks(tasks, error)

        def load_tasks():
            list_status_var.set("Reading Task Scheduler…")
            threading.Thread(target=lambda: results.put(list_windows_tasks()), daemon=True).start()
            win.after(150, poll_tasks)

        def select_task(_event=None):
            task = iid_to_task.get(next(iter(tv.selection()), None))
            if task is None or task.scheduled is None:
                return

            scheduled = task.scheduled
            path_var.set(scheduled.path)
            frequency_var.set(scheduled.frequency)
            weekday_var.set(scheduled.weekday)
            time_var.set(scheduled.time)
            status_var.set(
                "Loaded into the form above. Change it and click Create Scheduled Task "
                "to replace it" + (" and fix what needs attention." if task.problems() else ".")
            )

        def remove_task():
            task = iid_to_task.get(next(iter(tv.selection()), None))
            if task is None:
                status_var.set("Select a scheduled scan in the list to remove it.")
                return

            if not messagebox.askyesno(
                "Schedule Scans",
                f'Remove the scheduled scan "{task.name}"?',
                parent=win,
            ):
                return

            ok, message = schedule.delete_windows_task(task.name)
            if ok:
                status_var.set("Scheduled scan removed.")
            else:
                logger.warning("schtasks /Delete failed: %s", message)
                status_var.set(f"Nothing removed: {message}")
            load_tasks()

        tv.bind("<<TreeviewSelect>>", select_task)

        list_buttons = ttk.Frame(win, padding=(10, 0, 10, 6))
        list_buttons.pack(side=TOP, fill=X)
        ttk.Button(list_buttons, text="Remove Selected", command=remove_task).pack(side=LEFT)
        ttk.Button(list_buttons, text="Refresh List", command=load_tasks).pack(side=LEFT, padx=6)

        refresh()
        load_tasks()
