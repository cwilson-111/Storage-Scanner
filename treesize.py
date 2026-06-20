#!/usr/bin/env python3
"""
Storage Scanner - a disk usage analyzer for Windows.

Pick a drive or folder and it scans recursively, then shows every folder and
file in a tree sorted by size, with a percentage bar so the space hogs jump out.

Run:  python treesize.py
"""

import os
import sys
import threading
import queue
import subprocess
from tkinter import (
    Tk, Toplevel, ttk, StringVar, BOTH, X, Y, LEFT, RIGHT, TOP, BOTTOM, END, W, E,
    Menu, filedialog, messagebox,
)


def resource_path(name):
    """Resolve a bundled resource, whether running from source or a
    PyInstaller one-file build (which unpacks data into sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# --------------------------------------------------------------------------- #
# Scanning model
# --------------------------------------------------------------------------- #

class Node:
    """A file or directory in the scanned tree."""
    __slots__ = ("path", "name", "is_dir", "size", "children", "file_count", "error")

    def __init__(self, path, name, is_dir):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = 0            # total bytes (recursive for dirs)
        self.children = []       # list[Node]
        self.file_count = 0      # number of files contained (recursive)
        self.error = False       # True if we couldn't read this dir


def _worker_count():
    # Disk traversal is I/O-bound, so oversubscribe CPUs. On Windows each
    # scandir DirEntry already caches size/type, so the cost is mostly the
    # directory-enumeration syscalls — running many in parallel hides the wait.
    return min(32, (os.cpu_count() or 4) * 5)


def scan(path, progress_q, cancel_event, workers=None):
    """Scan `path` concurrently, returning the root Node.

    A pool of worker threads pulls directories off a shared queue and lists
    them in parallel; each discovered sub-directory is pushed back onto the
    queue. Because workers never block waiting on each other, there is no
    risk of pool-starvation deadlock no matter how deep the tree goes.
    Sizes are rolled up afterwards in a fast in-memory pass.

    Posts the running file count to `progress_q` and stops early if
    `cancel_event` is set.
    """
    path = os.path.abspath(path)
    name = path if path.endswith(os.sep) else os.path.basename(path) or path
    root = Node(path, name, is_dir=os.path.isdir(path))

    if not root.is_dir:
        try:
            root.size = os.path.getsize(path)
            root.file_count = 1
        except OSError:
            root.error = True
        progress_q.put(("progress", root.file_count))
        return root

    work = queue.Queue()
    work.put(root)

    scanned = [0]
    counter_lock = threading.Lock()

    def _scan_one(node):
        """List a single directory, attach children, queue sub-dirs."""
        if cancel_event.is_set():
            return
        try:
            entries = list(os.scandir(node.path))
        except OSError:
            node.error = True
            return

        local_files = 0
        for entry in entries:
            if cancel_event.is_set():
                return
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False

            child = Node(entry.path, entry.name, is_dir)
            node.children.append(child)  # only this worker touches node.children

            if is_dir:
                work.put(child)          # discovered later, sized in rollup
            else:
                try:
                    child.size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    child.error = True
                    child.size = 0
                child.file_count = 1
                local_files += 1

        if local_files:
            with counter_lock:
                scanned[0] += local_files
                count = scanned[0]
            progress_q.put(("progress", count))

    def _worker():
        while True:
            try:
                node = work.get()
            except Exception:
                return
            try:
                _scan_one(node)
            finally:
                work.task_done()

    n = workers or _worker_count()
    threads = [
        threading.Thread(target=_worker, daemon=True) for _ in range(n)
    ]
    for t in threads:
        t.start()

    work.join()  # block until every queued directory has been processed

    # Roll sizes/counts up the tree (iterative post-order; deep trees safe).
    _rollup(root)
    progress_q.put(("progress", scanned[0]))
    return root


def _rollup(root):
    """Sum child sizes/file counts into each directory, bottom-up."""
    stack = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if not node.is_dir:
            continue
        if processed:
            for child in node.children:
                node.size += child.size
                node.file_count += child.file_count
        else:
            stack.append((node, True))
            for child in node.children:
                if child.is_dir:
                    stack.append((child, False))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{num:,.0f} {unit}"
            return f"{num:,.1f} {unit}"
        num /= 1024.0


def bar(fraction, width=10):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class StorageScannerApp:
    def __init__(self, root):
        self.root = root
        root.title("Storage Scanner")
        root.geometry("960x640")
        try:
            root.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001 - icon is cosmetic; never fail over it
            pass

        self.progress_q = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_thread = None
        self.root_node = None
        self.node_by_iid = {}   # treeview iid -> Node

        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI construction --------------------------------------------------- #

    def _build_toolbar(self):
        bar_frame = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar_frame.pack(side=TOP, fill=X)

        ttk.Label(bar_frame, text="Location:").pack(side=LEFT)

        self.path_var = StringVar()
        self.path_combo = ttk.Combobox(
            bar_frame, textvariable=self.path_var, width=50,
            values=self._list_drives(),
        )
        self.path_combo.pack(side=LEFT, padx=6)
        self.path_combo.bind("<Return>", lambda e: self.start_scan())

        ttk.Button(bar_frame, text="Browse…", command=self.browse).pack(side=LEFT)
        self.scan_btn = ttk.Button(bar_frame, text="Scan", command=self.start_scan)
        self.scan_btn.pack(side=LEFT, padx=6)
        self.cancel_btn = ttk.Button(
            bar_frame, text="Cancel", command=self.cancel_scan, state="disabled"
        )
        self.cancel_btn.pack(side=LEFT)

        # Top-N largest files: dropdown for the count + a show button.
        self.top_btn = ttk.Button(
            bar_frame, text="Largest Files", command=self.show_top_files,
            state="disabled",
        )
        self.top_btn.pack(side=RIGHT)

        self.top_count_var = StringVar(value="25")
        self.top_count_combo = ttk.Combobox(
            bar_frame, textvariable=self.top_count_var, width=5, state="disabled",
            values=("25", "50", "100"),
        )
        self.top_count_combo.pack(side=RIGHT, padx=(0, 6))
        # Re-running with a new count is instant, so update live on selection.
        self.top_count_combo.bind(
            "<<ComboboxSelected>>", lambda e: self.show_top_files()
        )
        ttk.Label(bar_frame, text="Top").pack(side=RIGHT, padx=(0, 4))

        drives = self._list_drives()
        if drives:
            self.path_var.set(drives[0])

    def _build_tree(self):
        container = ttk.Frame(self.root, padding=(8, 4))
        container.pack(side=TOP, fill=BOTH, expand=True)

        columns = ("size", "percent", "items")
        self.tree = ttk.Treeview(
            container, columns=columns, show="tree headings", selectmode="browse"
        )
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("percent", text="% of Parent")
        self.tree.heading("items", text="Files")

        self.tree.column("#0", width=440, anchor=W, stretch=True)
        self.tree.column("size", width=110, anchor=E, stretch=False)
        self.tree.column("percent", width=200, anchor=W, stretch=False)
        self.tree.column("items", width=90, anchor=E, stretch=False)

        vsb = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.tree.tag_configure("dir", foreground="#1a5fb4")
        self.tree.tag_configure("error", foreground="#c01c28")

        # Lazy load children when a node is expanded.
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Right-click context menu.
        self.menu = Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open in Explorer", command=self._open_in_explorer)
        self.menu.add_command(label="Copy path", command=self._copy_path)
        self.tree.bind("<Button-3>", self._show_menu)

    def _build_statusbar(self):
        status = ttk.Frame(self.root, padding=(8, 2))
        status.pack(side=BOTTOM, fill=X)
        self.status_var = StringVar(value="Pick a drive or folder, then click Scan.")
        ttk.Label(status, textvariable=self.status_var, anchor=W).pack(
            side=LEFT, fill=X, expand=True
        )
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160)

    # -- Drive / folder selection ----------------------------------------- #

    @staticmethod
    def _list_drives():
        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = f"{letter}:\\"
            if os.path.exists(d):
                drives.append(d)
        return drives

    def browse(self):
        chosen = filedialog.askdirectory(title="Select a folder to analyze")
        if chosen:
            self.path_var.set(os.path.normpath(chosen))

    # -- Scan lifecycle ---------------------------------------------------- #

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        target = self.path_var.get().strip().strip('"')
        if not target or not os.path.exists(target):
            messagebox.showerror("Storage Scanner", f"Path does not exist:\n{target}")
            return

        # Reset state.
        self.cancel_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.root_node = None

        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.top_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self.progress.pack(side=RIGHT, padx=6)
        self.progress.start(12)
        self.status_var.set(f"Scanning {target} …")

        self.scan_thread = threading.Thread(
            target=self._scan_worker, args=(target,), daemon=True
        )
        self.scan_thread.start()
        self.root.after(100, self._poll_progress)

    def _scan_worker(self, target):
        try:
            node = scan(target, self.progress_q, self.cancel_event)
            self.progress_q.put(("done", node))
        except Exception as exc:  # noqa: BLE001 - report any scan failure to UI
            self.progress_q.put(("error", str(exc)))

    def _poll_progress(self):
        try:
            while True:
                kind, payload = self.progress_q.get_nowait()
                if kind == "progress":
                    self.status_var.set(f"Scanning … {payload:,} files counted")
                elif kind == "done":
                    self._finish_scan(payload)
                    return
                elif kind == "error":
                    self._finish_error(payload)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_progress)

    def _finish_scan(self, node):
        self.progress.stop()
        self.progress.pack_forget()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

        if self.cancel_event.is_set():
            self.status_var.set("Scan cancelled.")
            return

        self.root_node = node
        root_iid = self._insert_node("", node, parent_size=node.size or 1)
        self.tree.item(root_iid, open=True)
        self._populate_children(root_iid, node)
        self.top_btn.config(state="normal")
        self.top_count_combo.config(state="readonly")

        self.status_var.set(
            f"{node.path}  —  {human_size(node.size)} in "
            f"{node.file_count:,} files"
        )

    def _finish_error(self, msg):
        self.progress.stop()
        self.progress.pack_forget()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_var.set("Scan failed.")
        messagebox.showerror("Storage Scanner", f"Scan failed:\n{msg}")

    def cancel_scan(self):
        self.cancel_event.set()
        self.status_var.set("Cancelling …")

    # -- Treeview population (lazy) ---------------------------------------- #

    def _insert_node(self, parent_iid, node, parent_size):
        fraction = (node.size / parent_size) if parent_size else 0
        percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
        items = f"{node.file_count:,}" if node.is_dir else ""
        tags = ()
        if node.error:
            tags = ("error",)
        elif node.is_dir:
            tags = ("dir",)

        label = node.name + ("\\" if node.is_dir and not node.name.endswith("\\") else "")
        iid = self.tree.insert(
            parent_iid, END, text=label,
            values=(human_size(node.size), percent, items), tags=tags,
        )
        self.node_by_iid[iid] = node

        # Give expandable dirs a placeholder child so the [+] arrow appears.
        if node.is_dir and node.children:
            self.tree.insert(iid, END, text="…(loading)", tags=("placeholder",))
        return iid

    def _populate_children(self, parent_iid, node):
        # Remove placeholder if present.
        kids = self.tree.get_children(parent_iid)
        if len(kids) == 1 and self.tree.item(kids[0], "text") == "…(loading)":
            self.tree.delete(kids[0])
        elif kids:
            return  # already populated

        for child in sorted(node.children, key=lambda n: n.size, reverse=True):
            self._insert_node(parent_iid, child, parent_size=node.size or 1)

    def _on_open(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if node and node.is_dir:
            self._populate_children(iid, node)

    def _on_double_click(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if node and not node.is_dir:
            self._open_in_explorer()

    # -- Context menu actions ---------------------------------------------- #

    def _show_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _selected_node(self):
        return self.node_by_iid.get(self.tree.focus())

    def _reveal(self, path, is_dir):
        try:
            if is_dir:
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["explorer", "/select,", path])
        except Exception as exc:  # noqa: BLE001
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

    # -- Top 25 largest files --------------------------------------------- #

    def show_top_files(self, count=None):
        if not self.root_node:
            return
        if count is None:
            try:
                count = int(self.top_count_var.get())
            except (ValueError, AttributeError):
                count = 25

        # Collect every file in the scanned tree (iterative; deep-tree safe).
        files = []
        stack = [self.root_node]
        while stack:
            node = stack.pop()
            if node.is_dir:
                stack.extend(node.children)
            else:
                files.append(node)
        files.sort(key=lambda n: n.size, reverse=True)
        top = files[:count]

        # Reuse one window so changing the dropdown doesn't stack windows.
        existing = getattr(self, "_top_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._top_win = win
        win.title(f"Top {len(top)} Largest Files")
        win.geometry("820x520")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            pass

        ttk.Label(
            win, padding=(10, 8),
            text=f"Largest files under {self.root_node.path}"
                 "   (double-click to reveal in Explorer)",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("rank", "size", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("rank", text="#")
        tv.heading("size", text="Size")
        tv.heading("path", text="Path")
        tv.column("rank", width=44, anchor=E, stretch=False)
        tv.column("size", width=100, anchor=E, stretch=False)
        tv.column("path", width=640, anchor=W, stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        iid_to_path = {}
        for rank, node in enumerate(top, start=1):
            iid = tv.insert(
                "", END, values=(rank, human_size(node.size), node.path)
            )
            iid_to_path[iid] = node.path

        def on_double(_e):
            sel = tv.focus()
            if sel in iid_to_path:
                self._reveal(iid_to_path[sel], is_dir=False)

        tv.bind("<Double-1>", on_double)

    # -- Shutdown ---------------------------------------------------------- #

    def _on_close(self):
        self.cancel_event.set()
        self.root.destroy()


def main():
    root = Tk()
    try:
        ttk.Style().theme_use("vista")  # native-ish look on Windows
    except Exception:  # noqa: BLE001
        pass
    StorageScannerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
