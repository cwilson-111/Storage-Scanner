#!/usr/bin/env python3
"""
Storage Scanner - a disk usage analyzer for Windows.

Pick a drive or folder and it scans recursively, then shows every folder and
file in a tree sorted by size, with a percentage bar so the space hogs jump out.

Run:  python treesize.py
"""

import os
import sys
import ctypes
import threading
import queue
import subprocess
import hashlib
from ctypes import wintypes
from collections import defaultdict
from tkinter import (
    Tk, Toplevel, Canvas, ttk, StringVar, BOTH, X, Y, LEFT, RIGHT, TOP, BOTTOM,
    END, W, E, Menu, filedialog, messagebox,
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


_PARTIALS = "░▏▎▍▌▋▊▉█"   # 1/8-cell steps for a smooth, precise bar


def bar(fraction, width=14):
    fraction = max(0.0, min(1.0, fraction))
    full = fraction * width
    filled = int(full)
    cells = ["█"] * filled
    if filled < width:
        cells.append(_PARTIALS[int(round((full - filled) * 8))])
        cells.extend("░" * (width - filled - 1))
    return "".join(cells)


# --------------------------------------------------------------------------- #
# Filesystem operations
# --------------------------------------------------------------------------- #

# SHFileOperationW flags (shellapi.h).
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # the bit that routes deletes to the Recycle Bin
_FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),   # FILEOP_FLAGS is a WORD
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(path):
    """Send a file or folder to the Windows Recycle Bin (so it's recoverable).

    Uses the shell's SHFileOperationW with FOF_ALLOWUNDO — pure stdlib, no
    extra dependency. `pFrom` must be double-NUL terminated. Returns True on
    success, False otherwise.
    """
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = os.path.abspath(path) + "\x00\x00"
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


# --------------------------------------------------------------------------- #
# Theme — dark "cyber terminal" palette
# --------------------------------------------------------------------------- #

COLORS = {
    "bg":      "#0a0e16",   # window background (deep navy-black)
    "bg2":     "#0f1626",   # headings / scrollbar troughs
    "panel":   "#0d1320",   # tree body / input fields
    "border":  "#1c2740",
    "fg":      "#b8c6db",    # default text
    "muted":   "#48566e",    # disabled / placeholder
    "accent":  "#00e5ff",    # neon cyan (primary)
    "accent2": "#39ff14",    # neon green (highlights)
    "sel":     "#13294a",    # selection background
    "dir":     "#22d3ee",    # directories
    "file":    "#8aa0bd",    # files
    "error":   "#ff3864",    # unreadable / errors
    "stripe":  "#0b111e",    # alternating row
}

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI Semibold", 9)
FONT_MONO = ("Consolas", 10)
FONT_MONO_BOLD = ("Consolas", 10, "bold")
FONT_TITLE = ("Segoe UI", 20, "bold")


def heat_color(fraction):
    """Map 0..1 to a green→amber→red heat gradient (big hogs run hot)."""
    f = max(0.0, min(1.0, fraction))
    if f < 0.5:                       # green → amber
        t = f / 0.5
        c1, c2 = (0x39, 0xff, 0x14), (0xff, 0xe0, 0x00)
    else:                             # amber → red
        t = (f - 0.5) / 0.5
        c1, c2 = (0xff, 0xe0, 0x00), (0xff, 0x38, 0x64)
    r = round(c1[0] + (c2[0] - c1[0]) * t)
    g = round(c1[1] + (c2[1] - c1[1]) * t)
    b = round(c1[2] + (c2[2] - c1[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def apply_theme(root):
    """Style every ttk widget with the dark cyber palette."""
    C = COLORS
    style = ttk.Style()
    try:
        style.theme_use("clam")  # only theme that allows full recoloring
    except Exception:  # noqa: BLE001
        pass
    root.configure(bg=C["bg"])

    style.configure(".", background=C["bg"], foreground=C["fg"],
                    fieldbackground=C["panel"], font=FONT)
    style.configure("TFrame", background=C["bg"])
    style.configure("TLabel", background=C["bg"], foreground=C["fg"], font=FONT)
    style.configure("Accent.TLabel", background=C["bg"], foreground=C["accent"],
                    font=FONT_BOLD)

    # Buttons — flat terminal chips that invert on hover.
    style.configure("TButton", background=C["panel"], foreground=C["accent"],
                    bordercolor=C["border"], lightcolor=C["panel"],
                    darkcolor=C["panel"], relief="flat", padding=(12, 5),
                    font=FONT_BOLD)
    style.map("TButton",
              background=[("active", C["accent"]), ("disabled", C["bg"])],
              foreground=[("active", C["bg"]), ("disabled", C["muted"])],
              bordercolor=[("active", C["accent"])])

    # Comboboxes (+ their drop-down listbox via option db).
    style.configure("TCombobox", fieldbackground=C["panel"], background=C["panel"],
                    foreground=C["fg"], arrowcolor=C["accent"],
                    bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], selectbackground=C["sel"],
                    selectforeground=C["fg"], padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", C["panel"]), ("disabled", C["bg"])],
              foreground=[("disabled", C["muted"])],
              arrowcolor=[("disabled", C["muted"]), ("active", C["accent2"])])
    root.option_add("*TCombobox*Listbox.background", C["panel"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["bg"])
    root.option_add("*TCombobox*Listbox.font", FONT)

    # Treeview — monospaced rows so the bars line up perfectly.
    style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                    foreground=C["fg"], rowheight=24, font=FONT_MONO,
                    bordercolor=C["border"])
    style.configure("Treeview.Heading", background=C["bg2"], foreground=C["accent"],
                    relief="flat", font=FONT_BOLD, padding=(6, 6))
    style.map("Treeview.Heading",
              background=[("active", C["bg2"])],
              foreground=[("active", C["accent2"])])
    style.map("Treeview",
              background=[("selected", C["sel"])],
              foreground=[("selected", C["accent2"])])

    # Scrollbars.
    for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(orient, background=C["bg2"], troughcolor=C["bg"],
                        bordercolor=C["bg"], arrowcolor=C["accent"],
                        relief="flat")
        style.map(orient, background=[("active", C["accent"])])

    # Progressbar — solid neon sweep.
    style.configure("TProgressbar", background=C["accent"], troughcolor=C["panel"],
                    bordercolor=C["border"], lightcolor=C["accent"],
                    darkcolor=C["accent"])


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class StorageScannerApp:
    def __init__(self, root):
        self.root = root
        root.title("Storage Scanner")
        root.geometry("960x640")
        apply_theme(root)
        try:
            root.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001 - icon is cosmetic; never fail over it
            pass

        self.progress_q = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_thread = None
        self.root_node = None
        self.node_by_iid = {}   # treeview iid -> Node
        self._heat_tags = set()  # quantized heat tags configured so far
        self._sort_key = "size"  # "name" | "size" | "items"
        self._sort_reverse = True   # sizes default biggest-first
        
        #Progress bar for duplicates scan
        self.dup_progress_q = queue.Queue()
        self.dup_thread = None
        self.dup_cancel_event = threading.Event()


        self._build_header()
        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI construction --------------------------------------------------- #

    def _build_header(self):
        """A neon banner. tkinter has no blur, so the glow is faked by drawing
        the title several times in dim cyan at small offsets (a halo) with the
        bright text on top — a classic neon-sign trick."""
        self.header = Canvas(self.root, height=64, bg=COLORS["bg"],
                             highlightthickness=0, bd=0)
        self.header.pack(side=TOP, fill=X)
        self.header.bind("<Configure>", self._draw_header)

    def _draw_header(self, _event=None):
        cv = self.header
        cv.delete("all")
        C = COLORS
        width = cv.winfo_width()
        height = int(cv["height"])
        title = "◈  STORAGE SCANNER"
        x, y = 18, 30

        # Faint scanline grid behind everything — a CRT/terminal texture.
        step = 8
        if width > 1:
            for gx in range(0, width, step):
                cv.create_line(gx, 0, gx, height, fill="#0e1626")
            for gy in range(0, height, step):
                cv.create_line(0, gy, width, gy, fill="#0e1626")

        # Halo: far/dim ring, then near/brighter ring, then the crisp core.
        far = [(-2, -2), (2, -2), (-2, 2), (2, 2), (-3, 0), (3, 0), (0, -3), (0, 3)]
        near = [(-1, -1), (1, -1), (-1, 1), (1, 1), (-1, 0), (1, 0), (0, -1), (0, 1)]
        for dx, dy in far:
            cv.create_text(x + dx, y + dy, text=title, fill="#0a3a42",
                           font=FONT_TITLE, anchor=W)
        for dx, dy in near:
            cv.create_text(x + dx, y + dy, text=title, fill="#0f6d7a",
                           font=FONT_TITLE, anchor=W)
        cv.create_text(x, y, text=title, fill=C["accent"], font=FONT_TITLE, anchor=W)

        # Tagline + a glowing baseline rule under the banner.
        cv.create_text(x + 2, 52, text="// disk usage analyzer",
                       fill=C["muted"], font=FONT, anchor=W)
        if width > 1:
            cv.create_line(0, 62, width, 62, fill="#0f6d7a")
            cv.create_line(0, 63, width, 63, fill="#0a3a42")

    def _build_toolbar(self):
        bar_frame = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar_frame.pack(side=TOP, fill=X)

        ttk.Label(bar_frame, text="▸ LOCATION", style="Accent.TLabel").pack(side=LEFT)

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

        # Adding a tools menu dropdown
        self.tools_btn = ttk.Button(
            bar_frame,
            text="Tools ▼",
            command=self._show_tools_menu,
            state="disabled",
        )
        self.tools_btn.pack(side=LEFT, padx=6)

        self.tools_menu = Menu(self.root, tearoff=0)
        self.tools_menu.add_command(label="Find Duplicate Files", command=self.show_duplicates)
        self.tools_menu.add_command(label="File Types Breakdown", command=self.show_file_types)
        self.tools_menu.add_command(label="Largest Files", command=self.show_top_files)

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
        ttk.Label(bar_frame, text="TOP", style="Accent.TLabel").pack(side=RIGHT, padx=(0, 4))

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
        # Clickable headings sort that level (and every expanded level). The
        # percent column sorts by size — within a level they're equivalent.
        self.tree.heading("#0", text="Name",
                          command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size",
                          command=lambda: self._sort_by("size"))
        self.tree.heading("percent", text="% of Parent",
                          command=lambda: self._sort_by("size"))
        self.tree.heading("items", text="Files",
                          command=lambda: self._sort_by("items"))
        self._update_heading_arrows()

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

        # A row carries up to three tags that each set a *different* option, so
        # they stack cleanly: a heat tag (foreground), a type tag (font:
        # dirs bold), and a stripe tag (background). Errors override the
        # foreground to red. Heat tags are created lazily in _heat_tag().
        self.tree.tag_configure("dir", font=FONT_MONO_BOLD)
        self.tree.tag_configure("error", foreground=COLORS["error"],
                                font=FONT_MONO_BOLD)
        self.tree.tag_configure("placeholder", foreground=COLORS["muted"])
        self.tree.tag_configure("even", background=COLORS["panel"])
        self.tree.tag_configure("odd", background=COLORS["stripe"])

        # Lazy load children when a node is expanded.
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Right-click context menu.
        self.menu = Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open in Explorer", command=self._open_in_explorer)
        self.menu.add_command(label="Copy path", command=self._copy_path)
        self.menu.add_separator()
        self.menu.add_command(label="Delete (to Recycle Bin)",
                              command=self._delete_selected)
        self.tree.bind("<Button-3>", self._show_menu)

        # Keyboard: Delete recycles the selection, F5 re-scans.
        self.tree.bind("<Delete>", lambda e: self._delete_selected())
        self.root.bind("<F5>", lambda e: self.start_scan())

    def _build_statusbar(self):
        status = ttk.Frame(self.root, padding=(8, 2))
        status.pack(side=BOTTOM, fill=X)
        self.status_var = StringVar(value="Pick a drive or folder, then click Scan.")
        ttk.Label(status, textvariable=self.status_var, anchor=W).pack(
            side=LEFT, fill=X, expand=True
        )
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=220)

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
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self._start_indeterminate_progress()
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
        self._stop_progress()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

        if self.cancel_event.is_set():
            self.status_var.set("Scan cancelled.")
            return

        self.root_node = node
        root_iid = self._insert_node("", node, parent_size=node.size or 1)
        self.tree.item(root_iid, open=True)
        self._populate_children(root_iid, node)
        self.tools_btn.config(state="normal")
        self.top_count_combo.config(state="readonly")

        self.status_var.set(
            f"{node.path}  —  {human_size(node.size)} in "
            f"{node.file_count:,} files"
        )

    def _finish_error(self, msg):
        self._stop_progress()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.status_var.set("Scan failed.")
        messagebox.showerror("Storage Scanner", f"Scan failed:\n{msg}")

    def cancel_scan(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.status_var.set("Cancelling …")

    #-- Helper Methods for Progress Bar
    def _start_indeterminate_progress(self):
        """Show an animated progress bar when total work is unknown."""
        self.progress.config(mode="indeterminate", maximum=100, value=0)
        self.progress.pack(side=RIGHT, padx=6)
        self.progress.start(12)

    def _start_determinate_progress(self, maximum):
        """Show a percentage progress bar when total work is known."""
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=max(1, maximum), value=0)
        self.progress.pack(side=RIGHT, padx=6)

    def _update_determinate_progress(self, value):
        self.progress.config(value=value)

    def _stop_progress(self):
        self.progress.stop()
        self.progress.config(value=0)
        self.progress.pack_forget()
    # -- Treeview population (lazy) ---------------------------------------- #

    def _heat_tag(self, fraction):
        """Return a treeview tag whose foreground is the heat color for
        `fraction`, quantized to 25 buckets so we configure few tags."""
        bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
        name = f"heat{bucket}"
        if name not in self._heat_tags:
            self.tree.tag_configure(name, foreground=heat_color(bucket / 24))
            self._heat_tags.add(name)
        return name

    def _insert_node(self, parent_iid, node, parent_size, index=0):
        fraction = (node.size / parent_size) if parent_size else 0
        percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
        items = f"{node.file_count:,}" if node.is_dir else ""
        if node.error:
            tags = ["error"]
        else:
            tags = [self._heat_tag(fraction)]   # foreground = space-hog heat
            if node.is_dir:
                tags.append("dir")              # bold, keeps heat color
        tags.append("odd" if index % 2 else "even")

        label = node.name + ("\\" if node.is_dir and not node.name.endswith("\\") else "")
        iid = self.tree.insert(
            parent_iid, END, text=label,
            values=(human_size(node.size), percent, items), tags=tuple(tags),
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

        ordered = sorted(node.children, key=self._node_sort_key,
                         reverse=self._sort_reverse)
        for index, child in enumerate(ordered):
            self._insert_node(parent_iid, child, parent_size=node.size or 1,
                              index=index)

    def _node_sort_key(self, node):
        if self._sort_key == "name":
            return node.name.lower()
        if self._sort_key == "items":
            return node.file_count
        return node.size

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

    # -- Column sorting ---------------------------------------------------- #

    _HEADINGS = {"#0": "Name", "size": "Size",
                 "percent": "% of Parent", "items": "Files"}

    def _sort_by(self, key):
        """Handle a heading click: toggle direction if it's the active key,
        else switch to it (names ascend, sizes/counts descend by default)."""
        if key == self._sort_key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key = key
            self._sort_reverse = (key != "name")
        self._update_heading_arrows()
        self._resort_tree()

    def _update_heading_arrows(self):
        arrow = " ▼" if self._sort_reverse else " ▲"
        # The percent column is driven by the size sort, so it shares the mark.
        active_cols = {"size": ("size", "percent"),
                       "name": ("#0",), "items": ("items",)}[self._sort_key]
        for col, base in self._HEADINGS.items():
            text = base + (arrow if col in active_cols else "")
            self.tree.heading(col, text=text)

    def _resort_tree(self):
        """Re-order every already-populated level in place (preserves which
        nodes are expanded; lazy children sort on expand via _populate)."""
        def walk(parent_iid):
            self._sort_level(parent_iid)
            for iid in self.tree.get_children(parent_iid):
                node = self.node_by_iid.get(iid)
                if node and node.is_dir:
                    walk(iid)
        walk("")

    def _sort_level(self, parent_iid):
        kids = [k for k in self.tree.get_children(parent_iid)
                if k in self.node_by_iid]
        if not kids:
            return
        kids.sort(key=lambda iid: self._node_sort_key(self.node_by_iid[iid]),
                  reverse=self._sort_reverse)
        for index, iid in enumerate(kids):
            self.tree.move(iid, parent_iid, index)
            self._set_stripe(iid, index)

    def _set_stripe(self, iid, index):
        """Rewrite a row's even/odd background tag, keeping its other tags."""
        tags = [t for t in self.tree.item(iid, "tags") if t not in ("even", "odd")]
        tags.append("odd" if index % 2 else "even")
        self.tree.item(iid, tags=tuple(tags))

    def _refresh_row(self, iid):
        """Recompute a row's size / percent / files text from its node."""
        node = self.node_by_iid.get(iid)
        if not node:
            return
        parent_node = self.node_by_iid.get(self.tree.parent(iid))
        parent_size = (parent_node.size if parent_node else node.size) or 1
        fraction = (node.size / parent_size) if parent_size else 0
        percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
        items = f"{node.file_count:,}" if node.is_dir else ""
        self.tree.item(iid, values=(human_size(node.size), percent, items))

    # -- Delete to Recycle Bin --------------------------------------------- #

    def _forget_subtree(self, iid):
        """Drop an iid and all its descendants from the node map."""
        for child in self.tree.get_children(iid):
            self._forget_subtree(child)
        self.node_by_iid.pop(iid, None)

    def _delete_selected(self):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if not node:
            return
        kind = "folder" if node.is_dir else "file"
        if not messagebox.askyesno(
            "Delete to Recycle Bin",
            f"Send this {kind} to the Recycle Bin?\n\n{node.path}\n\n"
            f"{human_size(node.size)}"
            + (f" in {node.file_count:,} files" if node.is_dir else ""),
            icon="warning",
        ):
            return

        if not recycle(node.path):
            messagebox.showerror(
                "Storage Scanner",
                f"Could not delete:\n{node.path}\n\n"
                "It may be in use, protected, or require admin rights.",
            )
            return

        parent_iid = self.tree.parent(iid)
        parent_node = self.node_by_iid.get(parent_iid)

        # Subtract the removed size/count from every ancestor (incl. the root
        # row, whose parent is ""). root_node is the same object as its row.
        anc = parent_iid
        while anc:
            an = self.node_by_iid.get(anc)
            if an:
                an.size -= node.size
                an.file_count -= node.file_count
            anc = self.tree.parent(anc)
        if parent_node and node in parent_node.children:
            parent_node.children.remove(node)

        self._forget_subtree(iid)
        self.tree.delete(iid)

        # Siblings' "% of parent" and the ancestor sizes all shifted — refresh.
        for index, sib in enumerate(self.tree.get_children(parent_iid)):
            self._refresh_row(sib)
            self._set_stripe(sib, index)
        anc = parent_iid
        while anc:
            self._refresh_row(anc)
            anc = self.tree.parent(anc)

        if self.root_node:
            self.status_var.set(
                f"{self.root_node.path}  —  {human_size(self.root_node.size)} "
                f"in {self.root_node.file_count:,} files"
            )

    # -- Context menu actions ---------------------------------------------- #
    def _show_tools_menu(self):
        """Show the Tools dropdown under the Tools button."""
        try:
            x = self.tools_btn.winfo_rootx()
            y = self.tools_btn.winfo_rooty() + self.tools_btn.winfo_height()
            self.tools_menu.tk_popup(x, y)
        finally:
            self.tools_menu.grab_release()

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
        if not top:
            self.status_var.set("No files found.")
            return

        # Reuse one window so changing the dropdown doesn't stack windows.
        existing = getattr(self, "_top_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._top_win = win
        win.configure(bg=COLORS["bg"])
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

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        # Heat each row by its size relative to the largest file in the list.
        if not top:
            self.status_var.set("No files found.")
            return
        max_size = top[0].size or 1
        heat_seen = set()

        def heat_tag(fraction):
            bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
            name = f"heat{bucket}"
            if name not in heat_seen:
                tv.tag_configure(name, foreground=heat_color(bucket / 24))
                heat_seen.add(name)
            return name

        iid_to_path = {}
        for rank, node in enumerate(top, start=1):
            iid = tv.insert(
                "", END, values=(rank, human_size(node.size), node.path),
                tags=(heat_tag(node.size / max_size),
                      "odd" if rank % 2 else "even"),
            )
            iid_to_path[iid] = node.path

        def on_double(_e):
            sel = tv.focus()
            if sel in iid_to_path:
                self._reveal(iid_to_path[sel], is_dir=False)

        tv.bind("<Double-1>", on_double)

    # -- File-type breakdown ----------------------------------------------- #

    def show_file_types(self):
        if not self.root_node:
            return

        # Aggregate bytes + counts by lowercased extension across the tree.
        sizes = defaultdict(int)
        counts = defaultdict(int)
        stack = [self.root_node]
        while stack:
            node = stack.pop()
            if node.is_dir:
                stack.extend(node.children)
            else:
                ext = os.path.splitext(node.name)[1].lower() or "(no extension)"
                sizes[ext] += node.size
                counts[ext] += 1
        rows = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
        total = self.root_node.size or 1

        # Reuse one window so re-opening doesn't stack them.
        existing = getattr(self, "_types_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._types_win = win
        win.configure(bg=COLORS["bg"])
        win.title(f"File Types — {len(rows)} extensions")
        win.geometry("760x520")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            pass

        ttk.Label(
            win, padding=(10, 8),
            text=f"Space by file type under {self.root_node.path}",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("ext", "size", "percent", "files")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("ext", text="Type")
        tv.heading("size", text="Size")
        tv.heading("percent", text="% of Total")
        tv.heading("files", text="Files")
        tv.column("ext", width=150, anchor=W, stretch=False)
        tv.column("size", width=110, anchor=E, stretch=False)
        tv.column("percent", width=260, anchor=W, stretch=True)
        tv.column("files", width=90, anchor=E, stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        heat_seen = set()

        def heat_tag(fraction):
            bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
            name = f"heat{bucket}"
            if name not in heat_seen:
                tv.tag_configure(name, foreground=heat_color(bucket / 24))
                heat_seen.add(name)
            return name

        for index, (ext, size) in enumerate(rows):
            fraction = size / total
            percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
            tv.insert(
                "", END,
                values=(ext, human_size(size), percent, f"{counts[ext]:,}"),
                tags=(heat_tag(fraction), "odd" if index % 2 else "even"),
            )

  # -- Duplicate file finder --------------------------------------------- #

    def _hash_file(self, path, chunk_size=1024 * 1024):
        """Return a SHA-256 hash for a file, or None if it cannot be read."""
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    def _find_duplicate_files(self, progress_q=None, cancel_event=None):
        """Find duplicate files under the scanned root.

        Progress phases:
        1. Collect files by size.
        2. Hash only same-size files.
        """
        if not self.root_node:
            return []

        if cancel_event is None:
            cancel_event = threading.Event()

        all_files = []
        stack = [self.root_node]

        while stack:
            if cancel_event.is_set():
                return []

            node = stack.pop()
            if node.is_dir:
                stack.extend(node.children)
            else:
                if node.size > 0:
                    all_files.append(node)

        total_files = max(1, len(all_files))

        # Phase 1: group by size.
        by_size = defaultdict(list)

        for index, node in enumerate(all_files, start=1):
            if cancel_event.is_set():
                return []

            by_size[node.size].append(node)

            if progress_q and index % 100 == 0:
                progress_q.put((
                    "progress",
                    index,
                    total_files,
                    f"Checking file sizes … {index:,}/{total_files:,}"
                ))

        duplicate_size_groups = {
            size: nodes for size, nodes in by_size.items() if len(nodes) > 1
        }

        files_to_hash = []
        for nodes in duplicate_size_groups.values():
            files_to_hash.extend(nodes)

        total_hash_files = max(1, len(files_to_hash))

        if progress_q:
            progress_q.put((
                "progress",
                0,
                total_hash_files,
                f"Hashing possible duplicates … 0/{total_hash_files:,}"
            ))

        # Phase 2: hash same-size files.
        by_hash = defaultdict(list)

        for index, node in enumerate(files_to_hash, start=1):
            if cancel_event.is_set():
                return []

            digest = self._hash_file(node.path)
            if digest:
                by_hash[(node.size, digest)].append(node)

            if progress_q and (index % 10 == 0 or index == total_hash_files):
                progress_q.put((
                    "progress",
                    index,
                    total_hash_files,
                    f"Hashing possible duplicates … {index:,}/{total_hash_files:,}"
                ))

        duplicates = []
        for (size, digest), nodes in by_hash.items():
            if len(nodes) > 1:
                duplicates.append((size, digest, nodes))

        duplicates.sort(
            key=lambda item: item[0] * (len(item[2]) - 1),
            reverse=True,
        )

        return duplicates

    def show_duplicates(self):
        if not self.root_node:
            return

        if self.dup_thread and self.dup_thread.is_alive():
            messagebox.showinfo(
                "Storage Scanner",
                "Duplicate scan is already running."
            )
            return

        self.dup_cancel_event.clear()
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")

        total_files = max(1, self.root_node.file_count)
        self._start_determinate_progress(total_files)
        self.status_var.set("Preparing duplicate scan …")

        self.dup_thread = threading.Thread(
            target=self._duplicate_worker,
            daemon=True,
        )
        self.dup_thread.start()

        self.root.after(100, self._poll_duplicate_progress)

    def _duplicate_worker(self):
        try:
            duplicates = self._find_duplicate_files(
                progress_q=self.dup_progress_q,
                cancel_event=self.dup_cancel_event,
            )

            if self.dup_cancel_event.is_set():
                self.dup_progress_q.put(("cancelled", None))
            else:
                self.dup_progress_q.put(("done", duplicates))

        except Exception as exc:
            self.dup_progress_q.put(("error", str(exc)))

    def _poll_duplicate_progress(self):
        try:
            while True:
                msg = self.dup_progress_q.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _kind, current, total, text = msg
                    self.progress.config(maximum=max(1, total))
                    self._update_determinate_progress(current)
                    percent = (current / max(1, total)) * 100
                    self.status_var.set(f"{text}  ({percent:5.1f}%)")

                elif kind == "done":
                    _kind, duplicates = msg
                    self._stop_progress()
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self._show_duplicates_window(duplicates)
                    return

                elif kind == "cancelled":
                    self._stop_progress()
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self.status_var.set("Duplicate scan cancelled.")
                    return

                elif kind == "error":
                    _kind, error_msg = msg
                    self._stop_progress()
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self.status_var.set("Duplicate scan failed.")
                    messagebox.showerror(
                        "Storage Scanner",
                        f"Duplicate scan failed:\n{error_msg}"
                    )
                    return

        except queue.Empty:
            pass

        self.root.after(100, self._poll_duplicate_progress)
    
    def _show_duplicates_window(self,duplicates):

        existing = getattr(self, "_duplicates_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._duplicates_win = win
        win.configure(bg=COLORS["bg"])
        win.title(f"Duplicate Files — {len(duplicates)} groups")
        win.geometry("980x600")

        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

        total_wasted = sum(size * (len(nodes) - 1) for size, _digest, nodes in duplicates)

        ttk.Label(
            win,
            padding=(10, 8),
            text=(
                f"Duplicate files under {self.root_node.path}  —  "
                f"{len(duplicates):,} groups, potential cleanup: {human_size(total_wasted)}"
            ),
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("group", "size", "copies", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")

        tv.heading("group", text="Group")
        tv.heading("size", text="Size")
        tv.heading("copies", text="Copies")
        tv.heading("path", text="Path")

        tv.column("group", width=70, anchor=E, stretch=False)
        tv.column("size", width=110, anchor=E, stretch=False)
        tv.column("copies", width=70, anchor=E, stretch=False)
        tv.column("path", width=700, anchor=W, stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)

        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        tv.tag_configure("keep", foreground=COLORS["accent2"])
        tv.tag_configure("dupe", foreground=COLORS["fg"])

        iid_to_node = {}

        row_index = 0
        for group_num, (size, _digest, nodes) in enumerate(duplicates, start=1):
            # Sort shortest path first; usually the "original" is easier to inspect.
            nodes = sorted(nodes, key=lambda n: n.path.lower())

            for copy_index, node in enumerate(nodes, start=1):
                tag_type = "keep" if copy_index == 1 else "dupe"
                stripe = "odd" if row_index % 2 else "even"

                iid = tv.insert(
                    "",
                    END,
                    values=(
                        group_num,
                        human_size(size),
                        f"{copy_index}/{len(nodes)}",
                        node.path,
                    ),
                    tags=(tag_type, stripe),
                )
                iid_to_node[iid] = node
                row_index += 1

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def reveal_selected():
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self._reveal(node.path, is_dir=False)

        def copy_selected_path():
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self.root.clipboard_clear()
                self.root.clipboard_append(node.path)

        def delete_selected_duplicates():
            selected = list(tv.selection())
            nodes = [iid_to_node[iid] for iid in selected if iid in iid_to_node]

            if not nodes:
                return

            if not messagebox.askyesno(
                "Delete selected duplicates",
                f"Send {len(nodes)} selected file(s) to the Recycle Bin?\n\n"
                "Warning: this does not automatically protect one copy per group. "
                "Only delete files you intentionally selected.",
                icon="warning",
                parent=win,
            ):
                return

            deleted_count = 0
            failed = []

            for iid in selected:
                node = iid_to_node.get(iid)
                if not node:
                    continue

                if recycle(node.path):
                    deleted_count += 1
                    iid_to_node.pop(iid, None)
                    tv.delete(iid)
                    self._remove_node_from_scan_tree(node)
                else:
                    failed.append(node.path)

            self.status_var.set(
                f"Deleted {deleted_count:,} duplicate file(s) to Recycle Bin."
            )

            if failed:
                messagebox.showerror(
                    "Storage Scanner",
                    "Some files could not be deleted:\n\n" + "\n".join(failed[:10]),
                    parent=win,
                )

        ttk.Button(
            button_bar,
            text="Reveal in Explorer",
            command=reveal_selected,
        ).pack(side=LEFT)

        ttk.Button(
            button_bar,
            text="Copy Path",
            command=copy_selected_path,
        ).pack(side=LEFT, padx=6)

        ttk.Button(
            button_bar,
            text="Delete Selected",
            command=delete_selected_duplicates,
        ).pack(side=RIGHT)

        tv.bind("<Double-1>", lambda _e: reveal_selected())

        if not duplicates:
            self.status_var.set("No duplicate files found.")
        else:
            self.status_var.set(
                f"Found {len(duplicates):,} duplicate groups. "
                f"Potential cleanup: {human_size(total_wasted)}"
            )
    def _remove_node_from_scan_tree(self, target_node):
        """Remove a deleted file node from the in-memory scan tree and update sizes.

        This keeps the current scan somewhat accurate after deleting from the
        duplicate window. It does not fully refresh every visible tree row;
        press F5 to rescan for a perfect view.
        """
        if not self.root_node or target_node.is_dir:
            return

        stack = [(self.root_node, None)]

        while stack:
            node, parent = stack.pop()

            if node is target_node:
                if parent and target_node in parent.children:
                    parent.children.remove(target_node)

                # Subtract size and count from ancestors.
                self._subtract_from_ancestors(self.root_node, target_node)
                return

            if node.is_dir:
                for child in node.children:
                    stack.append((child, node))

    def _subtract_from_ancestors(self, current, target):
        """Subtract target's size/count from every ancestor containing it."""
        if not current.is_dir:
            return False

        found = False

        for child in current.children:
            if child is target:
                found = True
                break

            if child.is_dir and self._subtract_from_ancestors(child, target):
                found = True
                break

        if found:
            current.size -= target.size
            current.file_count -= target.file_count

        return found

    # -- Shutdown ---------------------------------------------------------- #

    def _on_close(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.root.destroy()

    



def main():
    root = Tk()
    StorageScannerApp(root)  # applies the dark cyber theme during construction
    root.mainloop()


if __name__ == "__main__":
    main()
