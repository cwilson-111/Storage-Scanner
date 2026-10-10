"""Follow the main window to a monitor with another DPI (Windows).

appearance.enable_dpi_awareness declares per-monitor awareness, so Windows
no longer stretches the app's bitmap on a monitor whose scale differs from
the one the app started on; the app has to redraw at the new DPI itself,
which Tk 8.6 doesn't do. follow_monitor_dpi checks the main window's DPI
whenever it moves or resizes, and rescale makes the windows look as they
would had the app started on that monitor: `tk scaling`, every named font,
px() and the ttk styles built from it (apply_theme), and the sizes the
windows were given through px(): window sizes, Treeview columns, wrap
lengths and canvas sizes.

Tk has one font scale for the whole app, so every window follows the main
window's monitor; a dialog dragged alone to another monitor keeps the main
window's scale there (sharp, but too big or too small).
"""

import ctypes

from storage_scanner import settings
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_WINDOWS

_GA_ROOT = 2  # GetAncestor: the top-level window that owns a Tk widget
BASE_DPI = 96  # 100%
_SIZE_SLOP_PX = 4  # how far off a size Tk set may come out (see follow_monitor_dpi)


def window_dpi(window):
    """The DPI of the monitor `window` is on, or None when Windows can't
    say (not Windows, before Windows 10 1607, or not mapped)."""
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        user32.GetDpiForWindow.argtypes = (ctypes.c_void_p,)
        hwnd = user32.GetAncestor(window.winfo_id(), _GA_ROOT)
        dpi = user32.GetDpiForWindow(hwnd) if hwnd else 0
    except (AttributeError, OSError):
        return None
    return dpi or None


def follow_monitor_dpi(root):
    """Rescale the app whenever the main window `root` lands on a monitor
    with another DPI. Without per-monitor awareness the DPI Windows reports
    never changes, so this never fires."""
    # base: the main window's size at 96 DPI, which the next move scales.
    # given: the size the last rescale gave it; settled: whether it has
    # arrived. Crossing monitors, Windows resizes the window a few times
    # before that size arrives, and it comes out a couple of pixels off
    # (1,201 wide asked, 1,199 got at 125%); read as the user's size, either
    # would add up with every move.
    seen = {"dpi": round(settings.ui_scale() * BASE_DPI), "base": None, "given": None}
    seen["settled"] = True

    def check(event):
        if event.widget is not root:  # the toplevel's binding sees its children too
            return
        dpi = window_dpi(root)
        scale = settings.ui_scale()
        size = (event.width, event.height)
        if not dpi or dpi == seen["dpi"]:
            given = seen["given"]
            near = (
                given is not None
                and max(abs(size[0] - given[0]), abs(size[1] - given[1])) <= _SIZE_SLOP_PX
            )
            if near:
                seen["settled"] = True
            elif seen["settled"]:  # resized since: the user's size
                seen["given"] = None
                seen["base"] = (size[0] / scale, size[1] / scale)
            return
        logger.info("Main window moved to a %d DPI monitor (was %d)", dpi, seen["dpi"])
        seen["dpi"] = dpi
        base = seen["base"]
        seen["given"] = rescale(root, dpi, root_size=base and (base[0] * scale, base[1] * scale))
        seen["settled"] = seen["given"] is None

    root.bind("<Configure>", check, add="+")


def rescale(root, dpi, root_size=None):
    """Make every window look as it would had the app started at `dpi`,
    then send <<DpiChanged>> to `root` for whatever draws with px() later.
    `root_size` is the main window's (width, height) to scale, if not the
    one it has now. Returns the size the main window was given, or None if
    its size was left to Tk (maximised, or sized by its contents)."""
    old_scale = settings.ui_scale()
    toplevels = [root] + [w for w in _descendants(root) if w.winfo_class() == "Toplevel"]
    # Windows sized by their contents grow with their fonts by themselves;
    # the ones given a size (geometry) keep it unless told otherwise.
    fixed = {
        w: (w.winfo_width(), w.winfo_height())
        for w in toplevels
        if w.wm_state() == "normal"
        and (w.winfo_width(), w.winfo_height()) != (w.winfo_reqwidth(), w.winfo_reqheight())
    }
    if root in fixed and root_size:
        fixed[root] = root_size
    root.tk.call("tk", "scaling", dpi / 72)
    settings.apply_theme(root)  # px(), fonts, row height, scrollbar arrows
    ratio = settings.ui_scale() / old_scale
    sizes = {window: (round(w * ratio), round(h * ratio)) for window, (w, h) in fixed.items()}
    if ratio != 1:
        for widget in _descendants(root):
            _scale_widget(widget, ratio)
        for window, (width, height) in sizes.items():
            window.geometry(f"{width}x{height}")
    root.event_generate("<<DpiChanged>>")
    return sizes.get(root) if ratio != 1 else None


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _scale_widget(widget, ratio):
    """The options the app sets through px(), times `ratio`. Paddings and
    the like are plain pixels at every DPI, so they're left alone."""
    kind = widget.winfo_class()
    if kind == "Treeview":
        _scale_columns(widget, ratio)
    elif kind == "Canvas":
        for option in ("width", "height"):
            widget[option] = round(int(widget[option]) * ratio)
    if "wraplength" in widget.keys():  # noqa: SIM118 - a widget isn't a dict
        wrap = str(widget.cget("wraplength"))
        if wrap.isdigit() and int(wrap):
            widget.configure(wraplength=round(int(wrap) * ratio))


def _scale_columns(tree, ratio):
    """A Treeview's column widths times `ratio`. When the columns are wider
    than the tree, setting a width makes Tk squeeze the stretchable columns
    (Name, in the main tree) down to their minimum width, 20 pixels, and
    they stay there; their minimum is held at the new width until the
    windows have their new sizes."""
    columns = ("#0", *tree.tk.splitlist(tree["columns"]))
    widths = {column: round(int(tree.column(column, "width")) * ratio) for column in columns}
    held = {
        column: tree.column(column, "minwidth")
        for column in columns
        if tree.tk.getboolean(tree.column(column, "stretch"))
    }
    for column in held:
        tree.column(column, minwidth=widths[column])
    for column, width in widths.items():
        tree.column(column, width=width)

    def release():
        if tree.winfo_exists():
            for column, minwidth in held.items():
                tree.column(column, minwidth=minwidth)

    if held:
        tree.after_idle(release)
