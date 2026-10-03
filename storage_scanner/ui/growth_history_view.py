"""The Storage Growth History window: two saved snapshots of a path compared
in a Summary and a Growth Details tab, plus its Anomalies and saved Scans.

HistoryMixin.show_growth_history (ui/history_window.py) works out everything
shown here from the history database and opens one of these with it.
Comparing two other snapshots or removing a scan goes back through the app,
which opens a fresh window.
"""

import os
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    TOP,
    E,
    Menu,
    StringVar,
    Toplevel,
    W,
    X,
    ttk,
)
from typing import TYPE_CHECKING

from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, IS_MACOS, resource_path
from storage_scanner.settings import COLORS, FONT_BOLD, px
from storage_scanner.ui.history_scans import build_scans_tab

if TYPE_CHECKING:
    from storage_scanner.ui.app_state import AppState

# A folder's growth type -> the row tag that colours it; anything else
# (an unchanged folder) is muted.
_STATUS_TAGS = {"Growing": "growing", "Shrinking": "shrinking"}


def _change_percent_text(previous_size, growth_percent):
    """A folder's change as a percentage, or for one only a single scan has
    (history keeps folders of 50 MB or more), which way it went: it may have
    appeared or gone, or only crossed 50 MB."""
    if growth_percent is not None:
        return f"{growth_percent:.1f}%"
    return "New / was <50 MB" if previous_size == 0 else "Gone / now <50 MB"


def _format_change(value, is_bytes=True):
    if value is None:
        return "—"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    if is_bytes and isinstance(value, (int, float)) and abs(value) >= 1024:
        return f"{sign}{human_size(abs(value))}"
    return f"{sign}{int(value):,}"


def _format_percent(value):
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def _summarize_folder_change(row):
    if not row:
        return "—"
    (
        folder_path,
        previous_size,
        current_size,
        growth_bytes,
        growth_percent,
        growth_type,
        file_count,
    ) = row
    percent_text = "new" if growth_percent is None else f"{growth_percent:.1f}%"
    return f"{os.path.basename(folder_path)} — {human_size(growth_bytes)} ({percent_text})"


class GrowthHistoryWindow:
    """One Storage Growth History window for `scan_path`, comparing the saved
    snapshots `newer_id` and `older_id`. `summary` is history's growth
    summary for that pair and `rows` its folder changes; `anomalies`,
    `history_count` and `folder_by_anomaly` fill the Anomalies tab and
    `scan_choices` the picker and the Scans tab. `win` is its Toplevel."""

    def __init__(
        self,
        app: "AppState",
        scan_path,
        header_text,
        scan_choices,
        newer_id,
        older_id,
        summary,
        rows,
        anomalies,
        history_count,
        folder_by_anomaly,
    ):
        self.app = app
        self.scan_path = scan_path

        self.win = Toplevel(app.root)
        self.win.configure(bg=COLORS["bg"])
        self.win.title("Storage Growth History")
        self.win.geometry(f"{px(980)}x{px(620)}")

        try:
            self.win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            logger.debug("Growth History window iconbitmap failed", exc_info=True)

        ttk.Label(
            self.win,
            padding=(10, 8),
            text=header_text,
            wraplength=px(940),
        ).pack(side=TOP, fill=X)

        self._build_snapshot_picker(scan_choices, newer_id, older_id)

        notebook = ttk.Notebook(self.win)
        notebook.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        summary_frame = ttk.Frame(notebook, padding=10)
        details_frame = ttk.Frame(notebook, padding=10)
        anomalies_frame = ttk.Frame(notebook, padding=10)
        scans_frame = ttk.Frame(notebook, padding=10)
        notebook.add(summary_frame, text="Summary")
        notebook.add(details_frame, text="Growth Details")
        notebook.add(
            anomalies_frame,
            text=f"Anomalies ({len(anomalies)})" if anomalies else "Anomalies",
        )
        notebook.add(scans_frame, text=f"Scans ({len(scan_choices)})")
        self._build_anomalies_tab(anomalies_frame, anomalies, history_count, folder_by_anomaly)
        build_scans_tab(
            scans_frame,
            scan_choices,
            lambda scan_id, date_text: app._remove_scan(scan_path, scan_id, date_text),
        )
        self._build_overview(summary_frame, summary)
        self._build_top_changes(summary_frame, rows)
        self._build_details_tab(details_frame, rows)

    # ----- building -----

    def _build_snapshot_picker(self, scan_choices, newer_id, older_id):
        """Let the user pick any two saved snapshots of this path to compare,
        instead of only ever seeing the two most recent (roadmap: 'compare
        any two snapshots, not only the latest two')."""
        picker = ttk.LabelFrame(self.win, text="Compare snapshots", padding=(10, 6))
        picker.pack(side=TOP, fill=X, padx=10, pady=(0, 6))

        if len(scan_choices) < 2:
            ttk.Label(
                picker,
                text="Scan this path again at a later date to unlock snapshot comparison.",
            ).pack(side=LEFT)
            return

        label_by_id = {
            scan_id: f"{created_at.replace('T', ' ')}  —  "
            f"{human_size(total_size)}, {file_count:,} files"
            for scan_id, created_at, total_size, file_count in scan_choices
        }
        self.id_by_label = {label: scan_id for scan_id, label in label_by_id.items()}
        labels = list(self.id_by_label.keys())  # already newest-first from list_scans_for_path

        ttk.Label(picker, text="Compare to:").pack(side=LEFT)
        self.newer_var = StringVar(value=label_by_id.get(newer_id, labels[0]))
        newer_combo = ttk.Combobox(
            picker,
            textvariable=self.newer_var,
            values=labels,
            state="readonly",
            width=42,
        )
        newer_combo.pack(side=LEFT, padx=(4, 12))

        ttk.Label(picker, text="Baseline:").pack(side=LEFT)
        self.older_var = StringVar(value=label_by_id.get(older_id, labels[min(1, len(labels) - 1)]))
        older_combo = ttk.Combobox(
            picker,
            textvariable=self.older_var,
            values=labels,
            state="readonly",
            width=42,
        )
        older_combo.pack(side=LEFT, padx=(4, 12))

        ttk.Button(picker, text="Compare", command=self.compare_picked).pack(side=RIGHT)

    def _build_anomalies_tab(self, frame, anomalies, history_count, folder_by_anomaly=None):
        """Populate the Anomalies tab: scan-to-scan size changes that were
        statistical outliers for this path's own history (see
        storage_scanner.anomaly_detection) — a lead worth checking, not a
        diagnosis. `folder_by_anomaly` (anomaly -> folder path or None)
        adds a best-effort "which folder" column, since an anomaly on its
        own only knows the root path's total changed, not where — see
        HistoryMixin._likely_folder_for_anomaly."""
        if history_count < 4:
            ttk.Label(
                frame,
                text=(
                    f"Not enough scan history yet to detect anomalies "
                    f"({history_count}/4 scans needed)."
                ),
            ).pack(side=TOP, anchor=W)
            return

        folder_by_anomaly = folder_by_anomaly or {}

        cols = ("date", "kind", "change", "folder")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("date", text="Date")
        tv.heading("kind", text="Type")
        tv.heading("change", text="What happened")
        tv.heading("folder", text="Likely folder")
        tv.column("date", width=px(140), anchor=W, stretch=False)
        tv.column("kind", width=px(80), anchor=W, stretch=False)
        tv.column("change", width=px(420), anchor=W, stretch=True)
        tv.column("folder", width=px(260), anchor=W, stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        # Both anomaly kinds are worth a second look — a mass-deletion-shaped
        # drop isn't "good news" just because it's a decrease, so this uses
        # the same warning/critical severity colors the heat scale uses,
        # not the "shrinking = good" convention below (which is about a
        # plain summary of direction, not a flagged statistical outlier).
        tv.tag_configure("spike", foreground=COLORS["error"])
        tv.tag_configure("drop", foreground=COLORS["warning"])

        if not anomalies:
            tv.insert(
                "", END, values=("—", "—", "No anomalies detected in this path's history.", "")
            )
            return

        # Only anomalies a folder was actually identified for get a
        # reveal action — nothing to open for "Not identified".
        self.anomaly_tv = tv
        self.iid_to_folder = {}

        for index, anomaly in enumerate(anomalies):
            date_text = anomaly.created_at.split("T")[0]
            folder_path = folder_by_anomaly.get(anomaly)
            iid = tv.insert(
                "",
                END,
                values=(
                    date_text,
                    anomaly.kind.capitalize(),
                    anomaly.message,
                    folder_path or "Not identified",
                ),
                tags=(anomaly.kind, "odd" if index % 2 else "even"),
            )
            if folder_path:
                self.iid_to_folder[iid] = folder_path

        tv.bind("<Double-1>", self.reveal_anomaly_folder)

        self.anomaly_menu = Menu(frame, tearoff=0)
        tv.bind("<Button-2>" if IS_MACOS else "<Button-3>", self.show_anomaly_menu)

    def _build_overview(self, summary_frame, summary):
        overview = ttk.LabelFrame(summary_frame, text="Overview", padding=10)
        overview.pack(fill=X, pady=(0, 10))

        metrics = [
            (
                "Current size",
                (
                    human_size(summary["current_size_bytes"])
                    if summary["current_size_bytes"] is not None
                    else "—"
                ),
            ),
            (
                "Previous size",
                (
                    human_size(summary["previous_size_bytes"])
                    if summary["previous_size_bytes"] is not None
                    else "—"
                ),
            ),
            ("Size change", _format_change(summary["size_change_bytes"])),
            ("Size change %", _format_percent(summary["size_change_percent"])),
            (
                "Current files",
                (
                    f"{summary['current_file_count']:,}"
                    if summary["current_file_count"] is not None
                    else "—"
                ),
            ),
            (
                "Previous files",
                (
                    f"{summary['previous_file_count']:,}"
                    if summary["previous_file_count"] is not None
                    else "—"
                ),
            ),
            (
                "File count change",
                _format_change(summary["file_count_change"], is_bytes=False),
            ),
            ("Tracked folders", f"{summary['tracked_folders']:,}"),
            ("New folders (or newly over 50 MB)", f"{summary['new_folders']:,}"),
            (
                "Largest growth folder",
                _summarize_folder_change(summary["largest_growth_folder"]),
            ),
            (
                "Largest shrink folder",
                _summarize_folder_change(summary["largest_shrink_folder"]),
            ),
        ]

        for index, (label, value) in enumerate(metrics):
            ttk.Label(overview, text=f"{label}:", font=FONT_BOLD).grid(
                row=index // 2, column=(index % 2) * 2, sticky=W, padx=(0, 8), pady=4
            )
            ttk.Label(overview, text=value).grid(
                row=index // 2, column=(index % 2) * 2 + 1, sticky=W, pady=4
            )

    def _build_top_changes(self, summary_frame, rows):
        changes_frame = ttk.LabelFrame(summary_frame, text="Top folder changes", padding=10)
        changes_frame.pack(fill=BOTH, expand=True)

        change_cols = ("folder", "change", "status")
        change_tv = ttk.Treeview(
            changes_frame, columns=change_cols, show="headings", selectmode="browse"
        )
        change_tv.heading("folder", text="Folder")
        change_tv.heading("change", text="Change")
        change_tv.heading("status", text="Status")
        change_tv.column("folder", width=px(420), anchor=W, stretch=True)
        change_tv.column("change", width=px(140), anchor=E, stretch=False)
        change_tv.column("status", width=px(100), anchor=W, stretch=False)

        change_tv.pack(fill=BOTH, expand=True)
        change_tv.tag_configure("even", background=COLORS["panel"])
        change_tv.tag_configure("odd", background=COLORS["stripe"])
        change_tv.tag_configure("growing", foreground=COLORS["warning"])
        change_tv.tag_configure("shrinking", foreground=COLORS["good"])
        change_tv.tag_configure("unchanged", foreground=COLORS["muted"])

        if not rows:
            change_tv.insert(
                "",
                END,
                values=("No previous scan found for this exact path.", "", ""),
                tags=("even",),
            )
            return

        for index, row in enumerate(rows[:10]):
            (
                folder_path,
                previous_size,
                current_size,
                growth_bytes,
                growth_percent,
                growth_type,
                file_count,
            ) = row
            status_tag = _STATUS_TAGS.get(growth_type, "unchanged")
            change_tv.insert(
                "",
                END,
                values=(
                    folder_path,
                    f"{human_size(growth_bytes)} "
                    f"({_change_percent_text(previous_size, growth_percent)})",
                    growth_type,
                ),
                tags=(status_tag, "odd" if index % 2 else "even"),
            )

    def _build_details_tab(self, details_frame, rows):
        details_frame_content = ttk.Frame(details_frame, padding=(0, 0, 0, 0))
        details_frame_content.pack(fill=BOTH, expand=True)

        cols = ("folder", "previous", "current", "growth", "percent", "status", "files")
        tv = ttk.Treeview(details_frame_content, columns=cols, show="headings", selectmode="browse")

        tv.heading("folder", text="Folder")
        tv.heading("previous", text="Previous")
        tv.heading("current", text="Current")
        tv.heading("growth", text="Growth")
        tv.heading("percent", text="% Growth")
        tv.heading("status", text="Status")
        tv.heading("files", text="Files")

        tv.column("folder", width=px(360), anchor=W, stretch=True)
        tv.column("previous", width=px(100), anchor=E, stretch=False)
        tv.column("current", width=px(100), anchor=E, stretch=False)
        tv.column("growth", width=px(100), anchor=E, stretch=False)
        tv.column("percent", width=px(90), anchor=E, stretch=False)
        tv.column("status", width=px(90), anchor=W, stretch=False)
        tv.column("files", width=px(80), anchor=E, stretch=False)

        vsb = ttk.Scrollbar(details_frame_content, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)

        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        details_frame_content.rowconfigure(0, weight=1)
        details_frame_content.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        tv.tag_configure("growing", foreground=COLORS["warning"])
        tv.tag_configure("shrinking", foreground=COLORS["good"])
        tv.tag_configure("unchanged", foreground=COLORS["muted"])

        if not rows:
            tv.insert(
                "",
                END,
                values=("No previous scan found for this exact path.", "", "", "", "", "", ""),
                tags=("even",),
            )
            return

        for index, row in enumerate(rows):
            (
                folder_path,
                previous_size,
                current_size,
                growth_bytes,
                growth_percent,
                growth_type,
                file_count,
            ) = row

            percent_text = _change_percent_text(previous_size, growth_percent)

            status_tag = _STATUS_TAGS.get(growth_type, "unchanged")

            stripe = "odd" if index % 2 else "even"

            tv.insert(
                "",
                END,
                values=(
                    folder_path,
                    human_size(previous_size),
                    human_size(current_size),
                    human_size(growth_bytes),
                    percent_text,
                    growth_type,
                    f"{file_count:,}",
                ),
                tags=(status_tag, stripe),
            )

    # ----- actions -----

    def compare_picked(self):
        """The picker's Compare: reopen the window on the two picked snapshots."""
        self.app.show_growth_history(
            compare_a_id=self.id_by_label[self.newer_var.get()],
            compare_b_id=self.id_by_label[self.older_var.get()],
        )

    def reveal_anomaly_folder(self, _event=None):
        folder_path = self.iid_to_folder.get(self.anomaly_tv.focus())
        if folder_path:
            self.app._reveal(folder_path, is_dir=True)

    def show_anomaly_menu(self, event):
        tv = self.anomaly_tv
        iid = tv.identify_row(event.y)
        if not iid or iid not in self.iid_to_folder:
            return
        tv.selection_set(iid)
        tv.focus(iid)
        self.anomaly_menu.delete(0, END)
        self.anomaly_menu.add_command(
            label=f"Reveal in {FILE_MANAGER_NAME}",
            command=self.reveal_anomaly_folder,
        )
        self.anomaly_menu.tk_popup(event.x_root, event.y_root)
