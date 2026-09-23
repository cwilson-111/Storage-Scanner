"""Export Results and Schedule Scans: getting scan data out of the app, and
running scans without it.

A mixin composed into StorageScannerApp (storage_scanner/app.py). The file
shapes come from storage_scanner/export.py (the same writers the headless
`--cli` uses), and every scheduler command comes from
storage_scanner/schedule.py — this file is only the windows around them.
"""

import os
from tkinter import (
    BOTH, BOTTOM, END, LEFT, RIGHT, TOP, StringVar, Text, Toplevel, W, X,
    filedialog, messagebox, ttk,
)

from storage_scanner import schedule
from storage_scanner.export import export_to_file
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_WINDOWS, resource_path
from storage_scanner.settings import COLORS

_EXPORT_FILE_TYPES = {
    "csv": [("CSV (one row per file and folder)", "*.csv")],
    "json": [("JSON (nested folder tree)", "*.json")],
}


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
        win.geometry("760x470")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Schedule Scans window iconbitmap failed", exc_info=True)

        ttk.Label(
            win, padding=(10, 8), style="Accent.TLabel",
            text="Scan a folder automatically and keep its growth history up to date",
        ).pack(side=TOP, fill=X)

        scheduler = "Windows Task Scheduler" if IS_WINDOWS else "cron"
        ttk.Label(
            win, padding=(10, 0, 10, 8), foreground=COLORS["muted"], wraplength=720,
            justify=LEFT,
            text=(
                f"Runs a headless scan through {scheduler} and saves it to scan history, "
                "exactly like a scan from this window, so Growth History, forecasts, "
                "anomalies and budgets include it. It runs as you, without admin rights, "
                "so folders only an administrator can read are skipped. The app does "
                "not need to be open."
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
        ttk.Entry(form, textvariable=path_var).grid(row=0, column=1, columnspan=4, sticky="ew", padx=6)

        def browse():
            chosen = filedialog.askdirectory(parent=win, initialdir=path_var.get() or None)
            if chosen:
                path_var.set(os.path.normpath(chosen))

        ttk.Button(form, text="Browse…", command=browse).grid(row=0, column=5)

        ttk.Label(form, text="Every").grid(row=1, column=0, sticky=W, pady=3)
        ttk.Combobox(
            form, textvariable=frequency_var, values=schedule.FREQUENCIES,
            state="readonly", width=8,
        ).grid(row=1, column=1, sticky=W, padx=6)
        ttk.Label(form, text="on").grid(row=1, column=2, sticky=W)
        weekday_combo = ttk.Combobox(
            form, textvariable=weekday_var, values=schedule.WEEKDAYS,
            state="readonly", width=6,
        )
        weekday_combo.grid(row=1, column=3, sticky=W, padx=6)
        ttk.Label(form, text="at (HH:MM, 24-hour)").grid(row=2, column=0, sticky=W, pady=3)
        ttk.Entry(form, textvariable=time_var, width=8).grid(row=2, column=1, sticky=W, padx=6)
        form.columnconfigure(4, weight=1)

        ttk.Label(
            win, padding=(10, 6, 10, 2),
            text="Scheduled task" if IS_WINDOWS else "Crontab line (add it with `crontab -e`)",
        ).pack(side=TOP, fill=X)

        preview = Text(
            win, height=5, wrap="word", bg=COLORS["panel"], fg=COLORS["fg"],
            relief="flat", padx=8, pady=6,
        )
        preview.pack(side=TOP, fill=BOTH, expand=True, padx=10)

        status_var = StringVar()
        ttk.Label(win, textvariable=status_var, padding=(10, 4), wraplength=720, justify=LEFT).pack(
            side=TOP, fill=X,
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
            when = (
                "Every day" if scheduled.frequency == "daily"
                else f"Every {scheduled.weekday.title()}"
            )
            return (
                f"Task: {schedule.task_name(scheduled)}\n"
                f"When: {when} at {scheduled.time}, or as soon as the PC is back on "
                "if that time was missed\n"
                f"Runs: {schedule.display_command(schedule.scan_command(scheduled))}"
            )

        def command_text(scheduled):
            if not IS_WINDOWS:
                return schedule.cron_line(scheduled)

            scheduled.validate()
            return schedule.display_command(schedule.scan_command(scheduled))

        def refresh(*_):
            weekday_combo.config(state="readonly" if frequency_var.get() == "weekly" else "disabled")
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
                    f"Scheduled \"{schedule.task_name(scheduled)}\". It appears in Task "
                    "Scheduler under that name; scheduling this folder again replaces it."
                )
            else:
                logger.warning("schtasks /Create failed: %s", message)
                messagebox.showerror(
                    "Schedule Scans", f"Task Scheduler refused the task:\n{message}", parent=win,
                )

        def remove_task():
            scheduled = current_schedule()

            if not messagebox.askyesno(
                "Schedule Scans",
                f"Remove the scheduled scan \"{schedule.task_name(scheduled)}\"?",
                parent=win,
            ):
                return

            ok, message = schedule.delete_windows_task(scheduled)
            status_var.set(
                "Scheduled scan removed." if ok
                else f"Nothing removed: {message or 'no scheduled scan for this folder'}"
            )

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        if IS_WINDOWS:
            ttk.Button(
                button_bar, text="Create Scheduled Task", style="Primary.TButton",
                command=create_task,
            ).pack(side=LEFT)
            ttk.Button(button_bar, text="Remove for This Folder", command=remove_task).pack(
                side=LEFT, padx=6,
            )
            ttk.Button(button_bar, text="Copy Command", command=copy_entry).pack(side=RIGHT)
        else:
            ttk.Button(
                button_bar, text="Copy Crontab Line", style="Primary.TButton", command=copy_entry,
            ).pack(side=LEFT)

        refresh()
