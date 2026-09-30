"""Growth History's Scans tab: every saved scan of the path, and "Remove
this scan" for one that doesn't belong in its history -- a test run, or a
scan that went wrong -- which would otherwise skew the trend, the forecast
and the anomalies for as long as history keeps it.

Only builds the widgets; what removing does is the caller's (HistoryMixin.
_remove_scan in history_window.py).
"""

from tkinter import END, LEFT, RIGHT, TOP, E, Menu, W, X, ttk

from storage_scanner.formatting import human_size
from storage_scanner.platform_support import IS_MACOS
from storage_scanner.settings import COLORS

REMOVE_LABEL = "Remove this scan"


def build_scans_tab(frame, scan_choices, on_remove):
    """Fill `frame` with the scans in `scan_choices` (history.
    list_scans_for_path rows: id, created_at, total_size, file_count;
    newest first). The button, the Delete key and the row menu call
    on_remove(scan_id, date_text) for the selected scan."""
    bar = ttk.Frame(frame)
    bar.pack(side=TOP, fill=X, pady=(0, 6))
    ttk.Label(
        bar,
        text="Removing a scan deletes it from this history only. Nothing on disk changes.",
    ).pack(side=LEFT)

    table = ttk.Frame(frame)
    table.pack(fill="both", expand=True)
    tv = ttk.Treeview(table, columns=("date", "size", "files"), show="headings")
    tv.configure(selectmode="browse")
    tv.heading("date", text="Scanned")
    tv.heading("size", text="Size")
    tv.heading("files", text="Files")
    tv.column("date", width=200, anchor=W, stretch=True)
    tv.column("size", width=140, anchor=E, stretch=False)
    tv.column("files", width=120, anchor=E, stretch=False)
    vsb = ttk.Scrollbar(table, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=vsb.set)
    tv.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    table.rowconfigure(0, weight=1)
    table.columnconfigure(0, weight=1)
    tv.tag_configure("even", background=COLORS["panel"])
    tv.tag_configure("odd", background=COLORS["stripe"])

    for index, (scan_id, created_at, total_size, file_count) in enumerate(scan_choices):
        tv.insert(
            "",
            END,
            iid=str(scan_id),
            values=(created_at.replace("T", " "), human_size(total_size), f"{file_count:,}"),
            tags=("odd" if index % 2 else "even",),
        )

    def remove_selected(_event=None):
        selection = tv.selection()
        if selection:
            on_remove(int(selection[0]), tv.set(selection[0], "date"))

    remove_button = ttk.Button(bar, text=REMOVE_LABEL, command=remove_selected)
    remove_button.pack(side=RIGHT)
    remove_button.state(["disabled"])

    def selection_changed(_event=None):
        remove_button.state(["!disabled"] if tv.selection() else ["disabled"])

    tv.bind("<<TreeviewSelect>>", selection_changed)
    tv.bind("<Delete>", remove_selected)

    row_menu = Menu(frame, tearoff=0)
    row_menu.add_command(label=REMOVE_LABEL, command=remove_selected)

    def show_row_menu(event):
        iid = tv.identify_row(event.y)
        if not iid:
            return
        tv.selection_set(iid)
        tv.focus(iid)
        row_menu.tk_popup(event.x_root, event.y_root)

    tv.bind("<Button-2>" if IS_MACOS else "<Button-3>", show_row_menu)
