"""Scan-history persistence glue and opening the Growth History window
(ui/growth_history_view.py) on what the history database holds.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import os
from tkinter import messagebox

from storage_scanner.anomaly_detection import detect_size_anomalies
from storage_scanner.forecasting import forecast_days_until_full, format_forecast_range
from storage_scanner.formatting import human_size
from storage_scanner.history_queries import (
    get_folder_growth,
    get_forecast_history,
    get_growth_summary,
    get_latest_drive_free,
    get_latest_scan_id,
    get_latest_scan_snapshot,
    get_most_recent_scan_path,
    get_previous_scan_id,
    get_scan_history,
    get_scan_ids_by_created_at,
    list_scans_for_path,
)
from storage_scanner.history_store import delete_scan
from storage_scanner.logging_setup import logger
from storage_scanner.scan_history import (
    collect_folder_sizes,
    drive_space,
    normalize_scan_path,
    record_scan,
)
from storage_scanner.scan_progress_model import FINISHED
from storage_scanner.ui.growth_history_view import GrowthHistoryWindow


class HistoryMixin:
    def _finish_history_save(
        self, current_scan_id, previous_scan_id, growth_rows, budget_breach, progress_token
    ):
        """
        Runs on the Tkinter UI thread after the background history save finishes.
        """
        self._scan_progress_end(FINISHED, progress_token)
        self.last_scan_id = current_scan_id
        self.last_previous_scan_id = previous_scan_id
        self.last_growth_rows = growth_rows

        if previous_scan_id:
            self.status_var.set(f"Scan complete. History saved. Growth rows: {len(growth_rows):,}")
            # Unless another scan has already replaced the tree this was for.
            if not self._scan_running() and self.root_node is not None:
                self._show_changes(previous_scan_id)
        else:
            self.status_var.set(
                "Scan complete. History saved. Scan the same path again to calculate growth."
            )

        if budget_breach:
            self._show_budget_banner([budget_breach])

    def _history_save_failed(self, exc, progress_token):
        """
        Runs on the Tkinter UI thread if history saving fails.
        """
        self._scan_progress_end(FINISHED, progress_token)
        self.last_growth_rows = []
        self.status_var.set(f"Scan complete, but history failed: {exc}")

    def _save_history_worker(self, node, progress_token):
        """
        Saves scan history in a background thread so the Tkinter UI does not freeze.
        `progress_token` is handed back to _scan_progress_end, so only the
        scan this save belongs to has its progress panel closed.
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
                    progress_token,
                ),
            )

        except Exception as exc:
            logger.exception("Saving scan history failed")
            # `except ... as exc` is auto-deleted at the end of this block,
            # so capture its message now — the lambda runs later, after exc
            # no longer exists.
            error_message = str(exc)
            self.root.after(0, lambda: self._history_save_failed(error_message, progress_token))

    def _format_forecast(self, forecast, free_as_of=""):
        """Render a Forecast namedtuple as one line — a range and an
        explicit confidence level, never a single number presented as
        certain (per the roadmap's own caution about forecasting).
        `free_as_of` says when the free space was measured, if not now."""
        if forecast.status == "insufficient_data":
            return f"Forecast: not enough history yet " f"({forecast.data_points}/3 scans needed)"
        if forecast.status == "not_growing":
            return "Forecast: not growing — no fill date to estimate"
        if forecast.status == "free_space_unknown":
            return "Forecast: can't read this drive's free space (is it connected?)"
        if forecast.days_estimate == 0:
            return f"Forecast: the drive has no free space left{free_as_of}"
        growth = human_size(forecast.bytes_per_day) + "/day"
        if not forecast.on_disk:
            growth += " in file sizes"
        return (
            f"Forecast: {human_size(forecast.free_bytes)} free{free_as_of}, used up in "
            f"{format_forecast_range(forecast)} if this path keeps growing {growth} "
            f"({forecast.confidence} confidence, {forecast.data_points} scans "
            f"over {forecast.span_days:,.0f} days, R²={forecast.r_squared:.2f})"
        )

    def _free_space_for_forecast(self, display_path, scan_path):
        """(free bytes, as-of text) for the forecast: the drive's free space
        now, or, when that can't be read (drive not connected, folder gone),
        what the newest scan of this path recorded, dated. (None, "") if
        neither is known."""
        space = drive_space(display_path)
        if space is not None:
            return space.free, ""
        latest = get_latest_drive_free(scan_path)
        if latest is None:
            return None, ""
        created_at, free = latest
        return free, f" at the scan of {created_at.split('T')[0]}"

    def _remove_scan(self, scan_path, scan_id, date_text):
        """Growth History's "Remove this scan": delete one saved scan of
        scan_path after asking, then show the window again without it."""
        win = self._growth_win
        if not messagebox.askyesno(
            "Remove this scan",
            f"Remove the scan of {date_text} from the history of {scan_path}?\n\n"
            "Its sizes are deleted from the history for good. Nothing on disk changes.",
            parent=win,
        ):
            return
        try:
            delete_scan(scan_id)
        except Exception as exc:
            logger.exception("Removing scan %s from history failed", scan_id)
            messagebox.showerror("Remove this scan", f"Couldn't remove it: {exc}", parent=win)
            return

        if scan_id in (self.last_scan_id, self.last_previous_scan_id):
            # This session's comparison lost a side: compare the newest two
            # scans that are left instead.
            newest = get_latest_scan_id(scan_path)
            previous = get_previous_scan_id(scan_path, newest) if newest else None
            self.last_scan_id, self.last_previous_scan_id = newest, previous
            self.last_growth_rows = get_folder_growth(newest, previous) if previous else []

        self.status_var.set(f"Removed the scan of {date_text} from the history of {scan_path}.")
        if get_latest_scan_id(scan_path) is None:
            win.destroy()
            return
        self.show_growth_history()

    def _likely_folder_for_anomaly(
        self, scan_path, anomaly, created_ats_in_order, scan_ids_by_created_at
    ):
        """Best-effort: which currently-tracked folder (>=50MB, see
        _collect_folder_sizes_for_history) most likely drove this anomaly's
        scan-to-scan change, found the same way the Growth Details tab
        already ranks folder changes (history_queries.get_folder_growth) — just for
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

        # Every row: a drop's folder sorts last, past any top-N cut.
        rows = get_folder_growth(current_id, previous_id, limit=None)
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

        # Everything the window shows is worked out before it's created, so
        # a failure here can't leave an empty window behind.
        free_bytes, free_as_of = self._free_space_for_forecast(display_path, scan_path)
        forecast_text = self._format_forecast(
            forecast_days_until_full(get_forecast_history(scan_path), free_bytes), free_as_of
        )
        full_history = get_scan_history(scan_path, limit=200)
        anomaly_list = detect_size_anomalies(full_history)
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

        existing = getattr(self, "_growth_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        view = GrowthHistoryWindow(
            self,
            scan_path,
            header_text=f"Growth history for {display_path}  —  {size_text}  |  {forecast_text}",
            scan_choices=scan_choices,
            newer_id=newer_id,
            older_id=older_id,
            summary=summary,
            rows=rows,
            anomalies=anomaly_list,
            history_count=len(full_history),
            folder_by_anomaly=folder_by_anomaly,
        )
        self._growth_win = view.win

    def _collect_folder_sizes_for_history(self, root_node):
        """Folders worth a history row — see scan_history.collect_folder_sizes."""
        return collect_folder_sizes(root_node)
