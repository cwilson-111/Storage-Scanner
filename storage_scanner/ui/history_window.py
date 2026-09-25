"""Scan-history persistence glue and the Growth History window.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
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
    messagebox,
    ttk,
)

from history import (
    get_folder_growth,
    get_growth_summary,
    get_latest_scan_id,
    get_latest_scan_snapshot,
    get_most_recent_scan_path,
    get_previous_scan_id,
    get_scan_history,
    get_scan_ids_by_created_at,
    list_scans_for_path,
)
from storage_scanner.anomaly_detection import detect_size_anomalies
from storage_scanner.forecasting import forecast_days_until_full
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, IS_MACOS, resource_path
from storage_scanner.scan_history import collect_folder_sizes, normalize_scan_path, record_scan
from storage_scanner.settings import COLORS, FONT_BOLD


class HistoryMixin:
    def _finish_history_save(self, current_scan_id, previous_scan_id, growth_rows, budget_breach):
        """
        Runs on the Tkinter UI thread after the background history save finishes.
        """
        self.last_scan_id = current_scan_id
        self.last_previous_scan_id = previous_scan_id
        self.last_growth_rows = growth_rows

        if previous_scan_id:
            self.status_var.set(f"Scan complete. History saved. Growth rows: {len(growth_rows):,}")
        else:
            self.status_var.set(
                "Scan complete. History saved. Scan the same path again to calculate growth."
            )

        if budget_breach:
            self._show_budget_banner([budget_breach])

    def _history_save_failed(self, exc):
        """
        Runs on the Tkinter UI thread if history saving fails.
        """
        self.last_growth_rows = []
        self.status_var.set(f"Scan complete, but history failed: {exc}")

    def _save_history_worker(self, node):
        """
        Saves scan history in a background thread so the Tkinter UI does not freeze.
        """
        try:
            recorded = record_scan(node)

            self.root.after(
                0,
                lambda: self._finish_history_save(
                    recorded.scan_id,
                    recorded.previous_scan_id,
                    recorded.growth_rows,
                    recorded.budget_breach,
                ),
            )

        except Exception as exc:
            logger.exception("Saving scan history failed")
            # `except ... as exc` is auto-deleted at the end of this block,
            # so capture its message now — the lambda runs later, after exc
            # no longer exists.
            error_message = str(exc)
            self.root.after(0, lambda: self._history_save_failed(error_message))

    def _format_change(self, value, is_bytes=True):
        if value is None:
            return "—"
        sign = "+" if value > 0 else "-" if value < 0 else ""
        if is_bytes and isinstance(value, (int, float)) and abs(value) >= 1024:
            return f"{sign}{human_size(abs(value))}"
        return f"{sign}{int(value):,}"

    def _format_percent(self, value):
        if value is None:
            return "—"
        return f"{value:+.1f}%"

    def _format_forecast(self, forecast):
        """Render a Forecast namedtuple as one line — a range and an
        explicit confidence level, never a single number presented as
        certain (per the roadmap's own caution about forecasting)."""
        if forecast.status == "insufficient_data":
            return f"Forecast: not enough history yet " f"({forecast.data_points}/3 scans needed)"
        if forecast.status == "not_growing":
            return "Forecast: not growing — no fill date to estimate"
        if forecast.days_estimate == 0:
            return "Forecast: drive is already full or over capacity"

        spread = forecast.days_pessimistic
        if spread is None or forecast.days_optimistic == spread:
            range_text = f"~{forecast.days_estimate:,} days"
        else:
            lo, hi = sorted([forecast.days_optimistic, spread])
            range_text = f"~{lo:,}–{hi:,} days"

        return (
            f"Forecast: full in {range_text} "
            f"({forecast.confidence} confidence, {forecast.data_points} scans "
            f"over {forecast.span_days:,.0f} days, R²={forecast.r_squared:.2f})"
        )

    def _likely_folder_for_anomaly(
        self, scan_path, anomaly, created_ats_in_order, scan_ids_by_created_at
    ):
        """Best-effort: which currently-tracked folder (>=50MB, see
        _collect_folder_sizes_for_history) most likely drove this anomaly's
        scan-to-scan change, found the same way the Growth Details tab
        already ranks folder changes (history.get_folder_growth) — just for
        the specific pair of scans this anomaly compares, instead of the
        two most recent.

        Returns None whenever a specific folder can't honestly be pointed
        to: a missing scan id, no tracked folder that actually moved in the
        anomaly's direction, or nothing but the root folder itself (which
        just restates the anomaly's own total, not a cause). This mirrors
        the rest of the app's "a lead worth checking, not a diagnosis"
        stance on anomalies — showing nothing is better than guessing.
        """
        try:
            index = created_ats_in_order.index(anomaly.created_at)
        except ValueError:
            return None
        if index == 0:
            return None

        current_id = scan_ids_by_created_at.get(anomaly.created_at)
        previous_id = scan_ids_by_created_at.get(created_ats_in_order[index - 1])
        if current_id is None or previous_id is None:
            return None

        rows = get_folder_growth(current_id, previous_id, limit=50)
        normalized_root = os.path.normcase(os.path.normpath(scan_path))
        candidates = [
            row for row in rows if os.path.normcase(os.path.normpath(row[0])) != normalized_root
        ]
        if not candidates:
            return None

        if anomaly.kind == "drop":
            folder_path, _prev, _curr, growth_bytes = min(candidates, key=lambda r: r[3])[:4]
            if growth_bytes >= 0:
                return None
        else:
            folder_path, _prev, _curr, growth_bytes = max(candidates, key=lambda r: r[3])[:4]
            if growth_bytes <= 0:
                return None

        return folder_path

    def _build_anomalies_tab(self, frame, anomalies, history_count, folder_by_anomaly=None):
        """Populate the Anomalies tab: scan-to-scan size changes that were
        statistical outliers for this path's own history (see
        storage_scanner.anomaly_detection) — a lead worth checking, not a
        diagnosis. `folder_by_anomaly` (anomaly -> folder path or None)
        adds a best-effort "which folder" column, since an anomaly on its
        own only knows the root path's total changed, not where — see
        _likely_folder_for_anomaly."""
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
        tv.column("date", width=140, anchor=W, stretch=False)
        tv.column("kind", width=80, anchor=W, stretch=False)
        tv.column("change", width=420, anchor=W, stretch=True)
        tv.column("folder", width=260, anchor=W, stretch=False)

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
        iid_to_folder = {}

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
                iid_to_folder[iid] = folder_path

        def reveal_selected(_event=None):
            folder_path = iid_to_folder.get(tv.focus())
            if folder_path:
                self._reveal(folder_path, is_dir=True)

        tv.bind("<Double-1>", reveal_selected)

        row_menu = Menu(frame, tearoff=0)

        def show_row_menu(event):
            iid = tv.identify_row(event.y)
            if not iid or iid not in iid_to_folder:
                return
            tv.selection_set(iid)
            tv.focus(iid)
            row_menu.delete(0, END)
            row_menu.add_command(
                label=f"Reveal in {FILE_MANAGER_NAME}",
                command=reveal_selected,
            )
            row_menu.tk_popup(event.x_root, event.y_root)

        tv.bind("<Button-2>" if IS_MACOS else "<Button-3>", show_row_menu)

    def _summarize_folder_change(self, row):
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

    # -- Show Growth Function ---------------------------------------------- #
    def _history_path_without_scan(self):
        """With nothing scanned this session: the path in the path box if
        it has saved scans, else the most recently scanned path, else None."""
        typed = self.path_var.get().strip().strip('"')
        if typed:
            candidate = normalize_scan_path(typed)
            if get_latest_scan_id(candidate) is not None:
                return candidate
        return get_most_recent_scan_path()

    def show_growth_history(self, compare_a_id=None, compare_b_id=None):
        """Show the Growth History window, comparing two arbitrary snapshots.

        Defaults to the most recent scan vs. the one before it (the normal
        post-scan case); the picker at the top of the window lets the user
        instead pick any two saved snapshots of this path and re-render.

        Works without a scan this session too: history lives in the
        database, including scans from earlier runs and earlier versions
        of the app, so it opens on the latest two saved snapshots.
        """
        if self.root_node is not None:
            display_path = self.root_node.path
            scan_path = normalize_scan_path(display_path)
            size_text = f"Current size: {human_size(self.root_node.size)}"
            default_newer, default_older = self.last_scan_id, self.last_previous_scan_id
            default_rows = getattr(self, "last_growth_rows", [])
        else:
            scan_path = self._history_path_without_scan()
            if scan_path is None:
                messagebox.showinfo(
                    "Growth History",
                    "No scan history yet. Scan a folder to start tracking how it grows.",
                )
                return
            display_path = scan_path  # stored normalized; no scan to take spelling from
            last_scanned_at, last_size, _files, _folders = get_latest_scan_snapshot(scan_path)
            size_text = f"Size at last scan ({last_scanned_at}): {human_size(last_size)}"
            default_newer = get_latest_scan_id(scan_path)
            default_older = get_previous_scan_id(scan_path, default_newer)
            default_rows = (
                get_folder_growth(default_newer, default_older, limit=50) if default_older else []
            )

        scan_choices = list_scans_for_path(scan_path)  # [(id, created_at, size, files), ...]

        newer_id = compare_a_id if compare_a_id is not None else default_newer
        older_id = compare_b_id if compare_b_id is not None else default_older

        if compare_a_id is not None or compare_b_id is not None:
            summary = get_growth_summary(newer_id, older_id)
            rows = get_folder_growth(newer_id, older_id, limit=50) if older_id else []
        else:
            rows = default_rows
            summary = get_growth_summary(newer_id, older_id)

        existing = getattr(self, "_growth_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._growth_win = win
        win.configure(bg=COLORS["bg"])
        win.title("Storage Growth History")
        win.geometry("980x620")

        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            logger.debug("Growth History window iconbitmap failed", exc_info=True)

        drive_capacity = self._get_drive_capacity_bytes(display_path)
        full_history = get_scan_history(scan_path, limit=200)
        forecast = forecast_days_until_full(full_history, drive_capacity)
        forecast_text = self._format_forecast(forecast)

        ttk.Label(
            win,
            padding=(10, 8),
            text=f"Growth history for {display_path}  —  {size_text}  |  {forecast_text}",
        ).pack(side=TOP, fill=X)

        self._build_snapshot_picker(win, scan_path, scan_choices, newer_id, older_id)

        notebook = ttk.Notebook(win)
        notebook.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        summary_frame = ttk.Frame(notebook, padding=10)
        details_frame = ttk.Frame(notebook, padding=10)
        anomalies_frame = ttk.Frame(notebook, padding=10)
        notebook.add(summary_frame, text="Summary")
        notebook.add(details_frame, text="Growth Details")
        anomaly_list = detect_size_anomalies(full_history)
        notebook.add(
            anomalies_frame,
            text=f"Anomalies ({len(anomaly_list)})" if anomaly_list else "Anomalies",
        )
        created_ats_in_order = [row[0] for row in full_history]
        scan_ids_by_created_at = get_scan_ids_by_created_at(scan_path, limit=200)
        folder_by_anomaly = {
            anomaly: self._likely_folder_for_anomaly(
                scan_path,
                anomaly,
                created_ats_in_order,
                scan_ids_by_created_at,
            )
            for anomaly in anomaly_list
        }
        self._build_anomalies_tab(
            anomalies_frame, anomaly_list, len(full_history), folder_by_anomaly
        )

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
            ("Size change", self._format_change(summary["size_change_bytes"])),
            ("Size change %", self._format_percent(summary["size_change_percent"])),
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
                self._format_change(summary["file_count_change"], is_bytes=False),
            ),
            ("Tracked folders", f"{summary['tracked_folders']:,}"),
            ("New folders", f"{summary['new_folders']:,}"),
            (
                "Largest growth folder",
                self._summarize_folder_change(summary["largest_growth_folder"]),
            ),
            (
                "Largest shrink folder",
                self._summarize_folder_change(summary["largest_shrink_folder"]),
            ),
        ]

        for index, (label, value) in enumerate(metrics):
            ttk.Label(overview, text=f"{label}:", font=FONT_BOLD).grid(
                row=index // 2, column=(index % 2) * 2, sticky=W, padx=(0, 8), pady=4
            )
            ttk.Label(overview, text=value).grid(
                row=index // 2, column=(index % 2) * 2 + 1, sticky=W, pady=4
            )

        changes_frame = ttk.LabelFrame(summary_frame, text="Top folder changes", padding=10)
        changes_frame.pack(fill=BOTH, expand=True)

        change_cols = ("folder", "change", "status")
        change_tv = ttk.Treeview(
            changes_frame, columns=change_cols, show="headings", selectmode="browse"
        )
        change_tv.heading("folder", text="Folder")
        change_tv.heading("change", text="Change")
        change_tv.heading("status", text="Status")
        change_tv.column("folder", width=420, anchor=W, stretch=True)
        change_tv.column("change", width=140, anchor=E, stretch=False)
        change_tv.column("status", width=100, anchor=W, stretch=False)

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
        else:
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
                if growth_type == "Growing":
                    status_tag = "growing"
                elif growth_type == "Shrinking":
                    status_tag = "shrinking"
                else:
                    status_tag = "unchanged"
                change_tv.insert(
                    "",
                    END,
                    values=(
                        folder_path,
                        f"{human_size(growth_bytes)} ({self._format_percent(growth_percent)})",
                        growth_type,
                    ),
                    tags=(status_tag, "odd" if index % 2 else "even"),
                )

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

        tv.column("folder", width=360, anchor=W, stretch=True)
        tv.column("previous", width=100, anchor=E, stretch=False)
        tv.column("current", width=100, anchor=E, stretch=False)
        tv.column("growth", width=100, anchor=E, stretch=False)
        tv.column("percent", width=90, anchor=E, stretch=False)
        tv.column("status", width=90, anchor=W, stretch=False)
        tv.column("files", width=80, anchor=E, stretch=False)

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
                values=(
                    "No previous scan found for this exact path.",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ),
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

            percent_text = "New" if growth_percent is None else f"{growth_percent:.1f}%"

            if growth_type == "Growing":
                status_tag = "growing"
            elif growth_type == "Shrinking":
                status_tag = "shrinking"
            else:
                status_tag = "unchanged"

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

    def _build_snapshot_picker(self, win, scan_path, scan_choices, newer_id, older_id):
        """Let the user pick any two saved snapshots of this path to compare,
        instead of only ever seeing the two most recent (roadmap: 'compare
        any two snapshots, not only the latest two')."""
        picker = ttk.LabelFrame(win, text="Compare snapshots", padding=(10, 6))
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
        id_by_label = {label: scan_id for scan_id, label in label_by_id.items()}
        labels = list(id_by_label.keys())  # already newest-first from list_scans_for_path

        ttk.Label(picker, text="Compare to:").pack(side=LEFT)
        newer_var = StringVar(value=label_by_id.get(newer_id, labels[0]))
        newer_combo = ttk.Combobox(
            picker,
            textvariable=newer_var,
            values=labels,
            state="readonly",
            width=42,
        )
        newer_combo.pack(side=LEFT, padx=(4, 12))

        ttk.Label(picker, text="Baseline:").pack(side=LEFT)
        older_var = StringVar(value=label_by_id.get(older_id, labels[min(1, len(labels) - 1)]))
        older_combo = ttk.Combobox(
            picker,
            textvariable=older_var,
            values=labels,
            state="readonly",
            width=42,
        )
        older_combo.pack(side=LEFT, padx=(4, 12))

        def do_compare():
            self.show_growth_history(
                compare_a_id=id_by_label[newer_var.get()],
                compare_b_id=id_by_label[older_var.get()],
            )

        ttk.Button(picker, text="Compare", command=do_compare).pack(side=RIGHT)

    # -- Duplicate file finder --------------------------------------------- #
    def _collect_folder_sizes_for_history(self, root_node):
        """Folders worth a history row — see scan_history.collect_folder_sizes."""
        return collect_folder_sizes(root_node)
