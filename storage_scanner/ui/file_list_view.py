"""A window listing files of the scanned tree, largest first.

Largest Files and a File Types extension's files (ui/file_windows.py) are
both one of these. Rows can be revealed, copied, added to the Cleanup Cart
or deleted through the delete service, singly or several at once; a delete
from any window reaches this one through forget_deleted.
"""

import contextlib
from tkinter import BOTH, END, TOP, E, Menu, StringVar, TclError, Toplevel, W, X, messagebox, ttk

from storage_scanner.delete_service import DeleteRequest
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, IS_MACOS, TRASH_NAME, resource_path
from storage_scanner.settings import COLORS, heat_color, px


class FileListWindow:
    """One window over the files `collect()` returns: a file_windows.FileList
    of files from `scan_tree`, the largest first, with the count and bytes
    of every match (the list itself may stop short of them).

    The Toplevel is kept on the app as `attr`, replacing any window already
    there, so a new scan can close it. `source` names the window in the
    Cleanup Cart and the audit ledger. `title(count, size)` words the title
    from the current count and bytes of every match. `recount_for`, when given, says
    whether a deleted node that wasn't one of the rows can still change the
    list (a folder, or a file that matches but wasn't shown); if so the list
    is collected again."""

    def __init__(self, app, attr, *, source, scan_tree, collect, title, heading, recount_for=None):
        self.app = app
        self.source = source
        self.scan_tree = scan_tree  # what every row here is from
        self.collect = collect
        self.title = title
        self.recount_for = recount_for
        self.iid_to_node = {}
        self.count = 0
        self.size = 0

        existing = getattr(app, attr, None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()
        self.win = Toplevel(app.root)
        setattr(app, attr, self.win)
        self.win.configure(bg=COLORS["bg"])
        self.win.geometry(f"{px(820)}x{px(520)}")
        try:
            self.win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("%s window iconbitmap failed", source, exc_info=True)

        ttk.Label(
            self.win,
            padding=(10, 8),
            text=f"{heading}   (double-click to reveal in {FILE_MANAGER_NAME}, "
            "right-click for more)",
        ).pack(side=TOP, fill=X)
        self.note_var = StringVar()
        self.note_label = ttk.Label(
            self.win, textvariable=self.note_var, style="Accent.TLabel", padding=(10, 0, 10, 8)
        )
        self.tv = self._build_table()
        self._build_menu()
        self.load()
        app._watch_deletions(self.win, self.forget_deleted)

    # ----- building -----

    def _build_table(self):
        frame = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)
        self.table_frame = frame

        cols = ("rank", "size", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        tv.heading("rank", text="#")
        tv.heading("size", text="Size")
        tv.heading("path", text="Path")
        tv.column("rank", width=px(44), anchor=E, stretch=False)
        tv.column("size", width=px(100), anchor=E, stretch=False)
        tv.column("path", width=px(640), anchor=W, stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        self.heat_seen = set()

        tv.bind("<Double-1>", lambda _e: self.reveal_focused())
        tv.bind("<Delete>", lambda _e: self.delete_selected())
        return tv

    def _build_menu(self):
        self.menu = Menu(self.win, tearoff=0)
        self.menu.add_command(label=f"Open in {FILE_MANAGER_NAME}", command=self.reveal_focused)
        self.menu.add_command(label="Copy Path", command=self.copy_selected_paths)
        self.menu.add_command(label="Add to Cart", command=self.add_selected_to_cart)
        self.menu.add_separator()
        self.menu.add_command(label=f"Delete (to {TRASH_NAME})", command=self.delete_selected)
        self.tv.bind("<Button-2>" if IS_MACOS else "<Button-3>", self.show_menu)
        for key in ("<Shift-F10>", "<App>", "<Menu>"):  # the context-menu keys
            # A key this platform's Tk doesn't name ("App" is Windows').
            with contextlib.suppress(TclError):
                self.tv.bind(key, lambda _e: self.show_menu_at_focus())

    # ----- rows -----

    def heat_tag(self, fraction):
        bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
        name = f"heat{bucket}"
        if name not in self.heat_seen:
            self.tv.tag_configure(name, foreground=heat_color(bucket / 24))
            self.heat_seen.add(name)
        return name

    def load(self):
        """(Re)fill the rows from a fresh collect(). Each row is heated by
        its size relative to the largest file listed."""
        listed = self.collect()
        self.tv.delete(*self.tv.get_children())
        self.iid_to_node.clear()
        self.count, self.size = listed.matched, listed.size
        max_size = (listed.nodes[0].size if listed.nodes else 0) or 1
        for rank, node in enumerate(listed.nodes, start=1):
            iid = self.tv.insert(
                "",
                END,
                values=(rank, human_size(node.size), node.path),
                tags=(self.heat_tag(node.size / max_size), "odd" if rank % 2 else "even"),
            )
            self.iid_to_node[iid] = node
        self.summarize()

    def summarize(self):
        """Refresh the title, and the note saying how many matches aren't
        listed (shown only while there are some)."""
        self.win.title(self.title(self.count, self.size))
        unlisted = self.count - len(self.iid_to_node)
        if unlisted > 0:
            self.note_var.set(
                f"Showing the largest {len(self.iid_to_node):,} of {self.count:,} files; "
                f"{unlisted:,} more aren't listed."
            )
            self.note_label.pack(side=TOP, fill=X, before=self.table_frame)
        else:
            self.note_label.pack_forget()

    def forget_deleted(self, deleted):
        """A delete from any window: drop the deleted rows and renumber the
        rest, or collect the list again when the delete reached files that
        weren't rows here."""
        gone = [iid for iid, node in self.iid_to_node.items() if deleted.covers(node)]
        gone_paths = {self.iid_to_node[iid].path for iid in gone}
        if self.recount_for is not None and any(
            node.path not in gone_paths and self.recount_for(node) for node in deleted.nodes
        ):
            self.load()
            return
        if not gone:
            return
        for iid in gone:
            node = self.iid_to_node.pop(iid)
            self.count -= 1
            self.size -= node.size
            self.tv.delete(iid)
        for rank, iid in enumerate(self.tv.get_children(), start=1):
            heat = self.tv.item(iid, "tags")[0]
            self.tv.item(iid, tags=(heat, "odd" if rank % 2 else "even"))
            self.tv.set(iid, "rank", rank)
        self.summarize()

    def selected_nodes(self):
        """The selected rows' files, in list order."""
        return [self.iid_to_node[iid] for iid in self.tv.selection() if iid in self.iid_to_node]

    # ----- menu -----

    def show_menu(self, event):
        iid = self.tv.identify_row(event.y)
        if not iid:
            return
        # Right-clicking inside a multi-row selection acts on all of it.
        if iid not in self.tv.selection():
            self.tv.selection_set(iid)
        self.tv.focus(iid)
        self.menu.tk_popup(event.x_root, event.y_root)

    def show_menu_at_focus(self):
        """Shift+F10 / the Menu key: the context menu under the focused row."""
        iid = self.tv.focus()
        box = self.tv.bbox(iid) if iid else None
        if not box:
            return "break"
        x, y, _width, height = box
        if iid not in self.tv.selection():
            self.tv.selection_set(iid)
        self.menu.tk_popup(self.tv.winfo_rootx() + x + 24, self.tv.winfo_rooty() + y + height)
        return "break"

    # ----- actions -----

    def reveal_focused(self):
        node = self.iid_to_node.get(self.tv.focus())
        if node:
            self.app._reveal(node.path, is_dir=False)

    def copy_selected_paths(self):
        nodes = self.selected_nodes()
        if nodes:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append("\n".join(node.path for node in nodes))

    def add_selected_to_cart(self):
        nodes = self.selected_nodes()
        for node in nodes:
            self.app.cart.add(node, self.source)
        if nodes:
            self.app._refresh_cart_indicator()

    def delete_selected(self):
        app, win = self.app, self.win
        nodes = self.selected_nodes()
        if not nodes or app._refuse_delete_during_scan(parent=win):
            return
        if len(nodes) == 1:
            question = (
                f"Send this file to the {TRASH_NAME}?\n\n{nodes[0].path}\n\n"
                f"{human_size(nodes[0].size)}"
            )
        else:
            total = sum(node.size for node in nodes)
            question = (
                f"Send {len(nodes):,} selected files ({human_size(total)}) to the {TRASH_NAME}?"
            )
        if not messagebox.askyesno(f"Delete to {TRASH_NAME}", question, icon="warning", parent=win):
            return
        # Deleted rows leave this list through forget_deleted.
        app._delete_nodes(
            [DeleteRequest(node, self.source, tree=self.scan_tree) for node in nodes], win
        )
