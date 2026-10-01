"""Main window: the tree widget, the status bar, and what a row's menu and
keys do (open, reveal, copy, delete, budget, add to cart).

A mixin composed into StorageScannerApp (storage_scanner/app.py). The rest
of the window is split out by job: ui/toolbar.py (toolbar, menus, drives,
elevation), ui/scan_lifecycle.py (running a scan), ui/scan_banners.py
(what a finished scan reports above the tree), ui/main_tree.py (the
finished tree's rows and sorting) and ui/live_tree.py (rows while scanning).
"""

import contextlib
import os
import subprocess
from tkinter import (
    BOTH,
    BOTTOM,
    LEFT,
    TOP,
    BooleanVar,
    E,
    Menu,
    StringVar,
    TclError,
    W,
    X,
    messagebox,
    simpledialog,
    ttk,
)

from history import set_budget
from storage_scanner.delete_service import DeleteRequest
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME,
    IS_LINUX,
    IS_MACOS,
    TRASH_NAME,
)
from storage_scanner.search import parse_size
from storage_scanner.settings import COLORS, FONT_MONO_BOLD


class MainWindowMixin:

    def _build_tree(self):
        container = ttk.Frame(self.root, padding=(8, 4))
        container.pack(side=TOP, fill=BOTH, expand=True)

        columns = ("size", "alloc", "percent", "items", "change")
        self.tree = ttk.Treeview(
            container, columns=columns, show="tree headings", selectmode="browse"
        )
        # Clickable headings sort that level (and every expanded level). The
        # percent/alloc columns sort by (logical) size — within a level
        # they track together closely enough to share one sort.
        self.tree.heading("#0", text="Name", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size", command=lambda: self._sort_by("size"))
        self.tree.heading("alloc", text="On Disk", command=lambda: self._sort_by("alloc"))
        self.tree.heading("percent", text="% of Parent", command=lambda: self._sort_by("size"))
        self.tree.heading("items", text="Files", command=lambda: self._sort_by("items"))
        self.tree.heading("change", text="Change", command=lambda: self._sort_by("change"))
        self._update_heading_arrows()

        self.tree.column("#0", width=440, anchor=W, stretch=True)
        self.tree.column("size", width=110, anchor=E, stretch=False)
        self.tree.column("alloc", width=110, anchor=E, stretch=False)
        self.tree.column("percent", width=200, anchor=W, stretch=False)
        self.tree.column("items", width=90, anchor=E, stretch=False)
        # Growth since the last saved scan of this path (P2-18); filled in
        # once this scan's history is saved (_show_changes).
        self.tree.column("change", width=150, anchor=E, stretch=False)

        vsb = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # Lists only the folders whose size changed since the last saved
        # scan of this path (P2-18); usable once the Change column is filled.
        self.changed_only_var = BooleanVar(master=self.root, value=False)
        self.changed_only_check = ttk.Checkbutton(
            container,
            text="Changed folders only",
            variable=self.changed_only_var,
            command=self._refilter_tree,
            state="disabled",
        )
        self.changed_only_check.grid(row=0, column=0, columnspan=2, sticky="e", pady=(0, 2))

        self.tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")
        container.rowconfigure(1, weight=1)
        container.columnconfigure(0, weight=1)

        # A row carries up to three tags that each set a *different* option, so
        # they stack cleanly: a heat tag (foreground), a type tag (font:
        # dirs bold), and a stripe tag (background). Errors override the
        # foreground to red. Heat tags are created lazily in _heat_tag().
        self.tree.tag_configure("dir", font=FONT_MONO_BOLD)
        self.tree.tag_configure("error", foreground=COLORS["error"], font=FONT_MONO_BOLD)
        self.tree.tag_configure("placeholder", foreground=COLORS["muted"])
        self.tree.tag_configure("link", foreground=COLORS["accent2"], font=FONT_MONO_BOLD)
        self.tree.tag_configure("cloud", foreground=COLORS["muted"], font=FONT_MONO_BOLD)
        self.tree.tag_configure("even", background=COLORS["panel"])
        self.tree.tag_configure("odd", background=COLORS["stripe"])

        # Lazy load children when a node is expanded.
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Double-1>", self._on_double_click)
        self._live_reset()
        self._live_bind(self.tree, vsb)

        # Right-click context menu.
        self.menu = Menu(self.root, tearoff=0)
        self.menu.add_command(label=f"Open in {FILE_MANAGER_NAME}", command=self._open_in_explorer)
        self.menu.add_command(label="Copy path", command=self._copy_path)
        self.menu.add_command(label="Set Budget…", command=self._set_budget_for_selected)
        self.menu.add_command(label="Add to Cart", command=self._add_selected_to_cart)
        self.menu.add_separator()
        self.menu.add_command(label=f"Delete (to {TRASH_NAME})", command=self._delete_selected)
        self.tree.bind("<Button-2>" if IS_MACOS else "<Button-3>", self._show_menu)
        for key in ("<Shift-F10>", "<App>", "<Menu>"):  # the context-menu keys
            # A key this platform's Tk doesn't name ("App" is Windows').
            with contextlib.suppress(TclError):
                self.tree.bind(key, lambda e: self._show_menu_at_focus())

        # Keyboard: Delete recycles the selection, F5 re-scans, Enter opens,
        # Backspace/Alt+Up goes to the parent, Ctrl+C copies the path,
        # Ctrl+F opens Search.
        self.tree.bind("<Delete>", lambda e: self._delete_selected())
        self.root.bind("<F5>", lambda e: self.start_scan())
        self.tree.bind("<Return>", lambda e: self._open_focused())
        for key in ("<BackSpace>", "<Alt-Up>"):
            self.tree.bind(key, lambda e: self._focus_parent())
        modifier = "Command" if IS_MACOS else "Control"
        self.tree.bind(f"<{modifier}-c>", lambda e: self._copy_path())
        self.root.bind(f"<{modifier}-f>", lambda e: self._open_search_if_scanned())

    def _build_statusbar(self):
        status = ttk.Frame(self.root, padding=(8, 2))
        # Packed ahead of the tree, so it and the strips packed after it keep
        # their height and a small window shrinks the tree instead.
        status.pack(side=BOTTOM, fill=X, before=self.tree.master)
        self._statusbar_frame = status
        self.status_var = StringVar(value="Pick a drive or folder, then click Scan.")
        ttk.Label(status, textvariable=self.status_var, anchor=W).pack(
            side=LEFT, fill=X, expand=True
        )
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=220)

        # Scan details strip: engine, timing and completeness of the last
        # finished scan, on two rows. Packed just above the status bar by
        # _show_scan_details, removed again when the next scan starts.
        self._scan_details_frame = ttk.Frame(self.root, padding=(8, 2))

    # -- Column sorting ---------------------------------------------------- #

    def _delete_selected(self):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if not node or self._refuse_delete_during_scan():
            return
        request = DeleteRequest(node, "Main tree")
        # Something that will be refused anyway (the scan's own root, a
        # drive, a system folder) isn't worth an "are you sure?" first.
        if self.delete_service.refusal(request) is None:
            kind = "folder" if node.is_dir else "file"
            if not messagebox.askyesno(
                f"Delete to {TRASH_NAME}",
                f"Send this {kind} to the {TRASH_NAME}?\n\n{node.path}\n\n"
                f"{human_size(node.size)}"
                + (f" in {node.file_count:,} files" if node.is_dir else ""),
                icon="warning",
            ):
                return
        self._delete_nodes([request])

    def _show_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _show_menu_at_focus(self):
        """Shift+F10 / the Menu key: the context menu under the focused row."""
        iid = self.tree.focus()
        box = self.tree.bbox(iid) if iid else None
        if not box:
            return "break"
        x, y, _width, height = box
        self.tree.selection_set(iid)
        self.menu.tk_popup(self.tree.winfo_rootx() + x + 24, self.tree.winfo_rooty() + y + height)
        return "break"

    def _open_focused(self):
        """Enter: a folder opens or closes, a file is shown in the file manager."""
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if iid in self._more_rows:
            self._show_more_rows(iid)
            return "break"
        if node is None:
            return "break"
        if not node.is_dir:
            self._open_in_explorer()
        elif self.tree.item(iid, "open"):
            self.tree.item(iid, open=False)
        else:
            self._on_open(None)  # fills the folder's rows, as clicking its arrow does
            self.tree.item(iid, open=True)
        return "break"

    def _focus_parent(self):
        """Backspace / Alt+Up: move to the row of the folder above."""
        parent = self.tree.parent(self.tree.focus())
        if parent:
            self.tree.focus(parent)
            self.tree.selection_set(parent)
            self.tree.see(parent)
        return "break"

    def _open_search_if_scanned(self):
        if self.root_node is not None and not self._scan_running():
            self.show_search_window()
        return "break"

    def _selected_node(self):
        return self.node_by_iid.get(self.tree.focus())

    def _add_selected_to_cart(self):
        node = self._selected_node()
        if node:
            self.cart.add(node, "Main tree")
            self._refresh_cart_indicator()

    def _reveal(self, path, is_dir):
        try:
            if IS_MACOS:
                if is_dir:
                    subprocess.run(["open", path])
                else:
                    subprocess.run(["open", "-R", path])
            elif IS_LINUX:
                # No portable "select this one file" flag across file
                # managers (Nautilus/Dolphin/Nemo/etc. each have their
                # own, if any, and xdg-open has none) -- opens the file's
                # own containing folder instead, same degraded-but-
                # functional fallback for a file as for a directory.
                subprocess.run(["xdg-open", path if is_dir else os.path.dirname(path)])
            elif is_dir:
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["explorer", "/select,", path])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not reveal %r", path, exc_info=True)
            messagebox.showerror("Storage Scanner", f"Could not open:\n{exc}")

    def _open_in_explorer(self):
        node = self._selected_node()
        if node:
            self._reveal(node.path, node.is_dir)

    def _copy_path(self):
        node = self._selected_node()
        if node:
            self.root.clipboard_clear()
            self.root.clipboard_append(node.path)

    def _set_budget_for_selected(self):
        node = self._selected_node()
        if not node:
            return
        if not node.is_dir:
            messagebox.showinfo(
                "Storage Scanner", "Budgets apply to folders, not individual files."
            )
            return

        response = simpledialog.askstring(
            "Set Budget",
            f"Alert when this folder's size exceeds a threshold.\n\n"
            f"{node.path}\nCurrently: {human_size(node.size)}\n\n"
            f"Enter a threshold (e.g. 50GB, 500MB):",
            parent=self.root,
        )
        if not response or not response.strip():
            return

        try:
            threshold_bytes = parse_size(response)
        except ValueError as exc:
            messagebox.showerror("Storage Scanner", str(exc))
            return
        if not threshold_bytes:
            messagebox.showerror("Storage Scanner", "Enter a size, e.g. 50GB.")
            return

        normalized = os.path.normcase(os.path.normpath(node.path))
        set_budget(normalized, threshold_bytes)
        self.status_var.set(f"Budget set: {node.path} → alert above {human_size(threshold_bytes)}")

    # -- Top 25 largest files --------------------------------------------- #
