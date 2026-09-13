"""App-wide constants: duplicate-scan exclusions, palette, fonts, ttk theme."""

from tkinter import ttk

from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_MACOS


_WINDOWS_DUPLICATE_EXCLUDES = (
    r"\Windows",
    r"\Program Files\WindowsApps",
    r"\Program Files\Common Files\Microsoft Shared",
    r"\Program Files\Microsoft Office",
    r"\Program Files (x86)\Microsoft",
    r"\ProgramData\Microsoft",
    r"\Recovery",
    r"\System Volume Information",
    r"\$Recycle.Bin",
)

_MACOS_DUPLICATE_EXCLUDES = (
    "/System",
    "/Library/Caches",
    "/Library/Application Support",
    "/private/var",
    "/.Spotlight-V100",
    "/.fseventsd",
    "/.Trash",
)

DEFAULT_DUPLICATE_EXCLUDES = (
    _MACOS_DUPLICATE_EXCLUDES if IS_MACOS else _WINDOWS_DUPLICATE_EXCLUDES
)

# --------------------------------------------------------------------------- #
# Theme — dark "cyber terminal" palette
# --------------------------------------------------------------------------- #

COLORS = {
    "bg":      "#05070c",
    "bg2":     "#08111f",
    "panel":   "#09111d",
    "border":  "#12324a",
    "fg":      "#c8f7ff",
    "muted":   "#536b82",
    "accent":  "#00e5ff",
    "accent2": "#7df9ff",
    "sel":     "#102f4f",
    "dir":     "#00ccff",
    "file":    "#93b8d8",
    "error":   "#ff3864",
    "stripe":  "#07101b",
}

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI Semibold", 9)
FONT_MONO = ("Consolas", 10)
FONT_MONO_BOLD = ("Consolas", 10, "bold")
FONT_TITLE = ("Segoe UI", 20, "bold")


def heat_color(fraction):
    """Map 0..1 to a green→amber→red heat gradient (big hogs run hot)."""
    f = max(0.0, min(1.0, fraction))
    if f < 0.5:                       # cyan → blue
        t = f / 0.5
        # c1, c2 = (0x39, 0xff, 0x14), (0xff, 0xe0, 0x00) for green -> amber
        c1,c2 = (0, 229, 255), (0, 102, 255)
    else:                             # blue → white 
        t = (f - 0.5) / 0.5
        # c1, c2 = (0xff, 0xe0, 0x00), (0xff, 0x38, 0x64) for amber -> red
        c1,c2 = (0,102,255), (255, 255, 255)
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
        logger.warning("ttk 'clam' theme unavailable, using default", exc_info=True)
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
