"""App-wide constants: duplicate-scan exclusions, palette, fonts, ttk theme."""

from tkinter import font as tkfont
from tkinter import ttk

from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_LINUX, IS_MACOS

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

# Standard FHS/XDG system paths, plus the two conventional per-user trash
# locations (XDG Trash spec's ~/.local/share/Trash, and the top-level
# .Trash-<uid> a removable/non-home filesystem uses instead) -- previously
# missing entirely, which meant is_protected_path() silently fell through
# to the *Windows* list on Linux (see settings.py's own DEFAULT_
# DUPLICATE_EXCLUDES below), matching nothing real on a Linux filesystem.
#
# is_protected_path()/DuplicatesMixin's own version both match these as a
# plain substring, not a path-segment boundary (same as the existing
# Windows/macOS lists above) -- a trailing "/" is deliberate on every
# short entry here (/etc, /usr, /lib, /dev, ...) so it can't also match
# an ordinary, unrelated user folder that just happens to start with the
# same letters (e.g. "/home/user/devops-notes" starts with "/dev", but
# not with "/dev/"). The longer, already-distinctive entries don't need
# it for the same reason the macOS/Windows lists above don't.
_LINUX_DUPLICATE_EXCLUDES = (
    "/proc/",
    "/sys/",
    "/dev/",
    "/boot/",
    "/usr/",
    "/lib/",
    "/lib64/",
    "/etc/",
    "/snap",
    "/var/lib",
    "/var/cache",
    "/.Trash",
    "/.local/share/Trash",
)

DEFAULT_DUPLICATE_EXCLUDES: "tuple[str, ...]"
if IS_MACOS:
    DEFAULT_DUPLICATE_EXCLUDES = _MACOS_DUPLICATE_EXCLUDES
elif IS_LINUX:
    DEFAULT_DUPLICATE_EXCLUDES = _LINUX_DUPLICATE_EXCLUDES
else:
    DEFAULT_DUPLICATE_EXCLUDES = _WINDOWS_DUPLICATE_EXCLUDES

# Size of each window the duplicate finder hashes: the first, middle, and
# last DUPLICATE_HASH_CHUNK_BYTES of a file (see DuplicatesMixin). Files up
# to 3x this size are compared byte-for-byte; larger ones are a sampled
# match (see cleanup_recommendations.is_sampled_duplicate).
DUPLICATE_HASH_CHUNK_BYTES = 1024 * 1024

# --------------------------------------------------------------------------- #
# Theme — "Structural Light": a light, data-tool palette. Fine hairlines and
# one confident blue instead of neon glow — see After Jarvis (the palette
# comparison put together while choosing this) for the reasoning. The dark
# palette keeps the same roles; storage_scanner/appearance.py picks one.
# --------------------------------------------------------------------------- #

LIGHT_COLORS = {
    "bg": "#f6f7f9",  # window background
    "bg2": "#eef1f6",  # header/heading chrome
    "panel": "#ffffff",  # content surfaces (tree rows, cards)
    "border": "#e3e6eb",
    "fg": "#1a1d23",  # primary text
    "muted": "#6b7280",  # secondary text
    "accent": "#3454d1",  # primary interactive blue
    "accent2": "#22398f",  # deeper navy — emphasis (links, keeper rows)
    "sel": "#dbe4f7",  # selected-row background
    "warning": "#d97706",  # caution: review candidates, growth, mid-heat
    "good": "#1a8754",  # positive: shrinking / improvement
    "error": "#c0331f",  # critical: errors, top-heat, spikes
    "stripe": "#fafbfc",  # subtle zebra striping
    "heat_low": "#9aa1b0",  # heat_color's three stops: small items stay quiet
    "heat_mid": "#d97706",
    "heat_high": "#c0331f",
}

DARK_COLORS = {
    "bg": "#1e1f22",
    "bg2": "#2b2d31",
    "panel": "#25272b",
    "border": "#3a3d44",
    "fg": "#e6e8eb",
    "muted": "#9aa1ac",
    "accent": "#4c6ef5",  # still carries white button text
    "accent2": "#a9bcff",  # light enough to read on the dark rows
    "sel": "#2f3d66",
    "warning": "#f0a030",
    "good": "#3fb97a",
    "error": "#ff6b5b",
    "stripe": "#2a2c30",
    "heat_low": "#7d8590",
    "heat_mid": "#f0a030",
    "heat_high": "#ff6b5b",
}

# The palette in use. Every module reads colours from this dict when it
# builds a widget, so use_palette must run before the first window.
COLORS = dict(LIGHT_COLORS)


def use_palette(name):
    """Switch COLORS to "light" or "dark" in place."""
    COLORS.clear()
    COLORS.update(DARK_COLORS if name == "dark" else LIGHT_COLORS)


# How much bigger than at 96 DPI the screen draws things: Tk's own scaling
# (pixels per point) over its 96-DPI value. apply_theme sets it, and
# ui/dpi_follow.py when the main window moves to a monitor with another DPI.
_UI_SCALE = 1.0


def px(pixels):
    """A size given in pixels at 96 DPI (100%), in pixels on this screen.
    Tk scales fonts for the screen's DPI by itself, but not sizes given in
    pixels: column widths, window sizes, wrap lengths."""
    return round(pixels * _UI_SCALE)


def ui_scale():
    """The scale px() uses: 1.0 at 96 DPI, 1.25 at 120, ..."""
    return _UI_SCALE


# Tkinter can only use fonts actually installed on the OS — there's no
# @font-face equivalent — so this picks each platform's native modern UI
# font rather than hardcoding one name and silently falling back on
# whichever platform doesn't have it.
#
# The app's fonts are named Tk fonts that apply_theme creates. A widget
# given a name follows the font when it's reconfigured, which is how the
# whole window follows a DPI change (ui/dpi_follow.py); a font given as a
# (family, size) tuple keeps the pixel size it got when first used.
FONT = "StorageScannerFont"
FONT_BOLD = "StorageScannerFontBold"
FONT_MONO = "StorageScannerMono"
FONT_MONO_BOLD = "StorageScannerMonoBold"

if IS_MACOS:
    _FONT_SPECS = {
        FONT: {"family": "Helvetica Neue", "size": 12},
        FONT_BOLD: {"family": "Helvetica Neue", "size": 12, "weight": "bold"},
        FONT_MONO: {"family": "Menlo", "size": 11},
        FONT_MONO_BOLD: {"family": "Menlo", "size": 11, "weight": "bold"},
    }
else:
    _FONT_SPECS = {
        FONT: {"family": "Segoe UI", "size": 9},
        FONT_BOLD: {"family": "Segoe UI Semibold", "size": 9},
        FONT_MONO: {"family": "Consolas", "size": 10},
        FONT_MONO_BOLD: {"family": "Consolas", "size": 10, "weight": "bold"},
    }


def refresh_fonts(root):
    """Create the app's named fonts in `root`'s interpreter, or re-size
    every named font (the app's and Tk's own, such as TkDefaultFont) for
    the current `tk scaling`. Tk computes a font's pixel size only when
    the font is configured, so this is what makes widgets follow a DPI
    change."""
    existing = set(root.tk.splitlist(root.tk.call("font", "names")))
    for name in existing - set(_FONT_SPECS):
        font = tkfont.nametofont(name, root=root)
        font.configure(size=font.cget("size"))
    for name, spec in _FONT_SPECS.items():
        if name in existing:
            tkfont.nametofont(name, root=root).configure(**spec)
        else:
            font = tkfont.Font(root=root, name=name, **spec)
            font.delete_font = False  # the name outlives this Python object


def heat_color(fraction):
    """Map 0..1 to a calm→amber→red heat gradient (big hogs run hot).

    A semantic scale, deliberately not built from the UI's own accent blue
    (that's reserved for actions/chrome) — small items stay a quiet
    neutral, and only real space hogs earn the warning/critical colors.
    """
    f = max(0.0, min(1.0, fraction))
    neutral, warning, critical = (
        _rgb(COLORS["heat_low"]),
        _rgb(COLORS["heat_mid"]),
        _rgb(COLORS["heat_high"]),
    )
    if f < 0.5:
        t = f / 0.5
        c1, c2 = neutral, warning
    else:
        t = (f - 0.5) / 0.5
        c1, c2 = warning, critical
    r = round(c1[0] + (c2[0] - c1[0]) * t)
    g = round(c1[1] + (c2[1] - c1[1]) * t)
    b = round(c1[2] + (c2[2] - c1[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def contrast_text_color(hex_color):
    """Black-ish or white text, whichever reads better against
    `hex_color` — computed from relative luminance rather than guessed
    per-case, so it stays correct if the heat/fill colors above change
    (and whichever palette is in use)."""
    r, g, b = _rgb(hex_color)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return LIGHT_COLORS["fg"] if luminance > 0.55 else "#ffffff"


def apply_theme(root):
    """Style every ttk widget with the palette in COLORS, record how much
    this screen scales things (px), and size the fonts for it. Runs again
    when the DPI changes (ui/dpi_follow.py)."""
    global _UI_SCALE
    _UI_SCALE = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
    refresh_fonts(root)
    C = COLORS
    style = ttk.Style()
    try:
        style.theme_use("clam")  # only theme that allows full recoloring
    except Exception:  # noqa: BLE001
        logger.warning("ttk 'clam' theme unavailable, using default", exc_info=True)
    root.configure(bg=C["bg"])

    style.configure(
        ".", background=C["bg"], foreground=C["fg"], fieldbackground=C["panel"], font=FONT
    )
    style.configure("TFrame", background=C["bg"])
    style.configure("TLabel", background=C["bg"], foreground=C["fg"], font=FONT)
    style.configure("Accent.TLabel", background=C["bg"], foreground=C["accent"], font=FONT_BOLD)

    # Buttons — flat chips with a hairline border; the primary blue only
    # shows up on hover/press, not as a permanent fill.
    style.configure(
        "TButton",
        background=C["panel"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["panel"],
        darkcolor=C["panel"],
        relief="flat",
        padding=(12, 5),
        font=FONT,
    )
    style.map(
        "TButton",
        background=[("active", C["accent"]), ("disabled", C["bg2"])],
        foreground=[("active", "#ffffff"), ("disabled", C["muted"])],
        bordercolor=[("active", C["accent"])],
    )

    # The one permanently-filled button in the whole app — reserved for the
    # single primary action (Scan), so it still means something.
    style.configure(
        "Primary.TButton",
        background=C["accent"],
        foreground="#ffffff",
        bordercolor=C["accent"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        relief="flat",
        padding=(12, 5),
        font=FONT_BOLD,
    )
    style.map(
        "Primary.TButton",
        background=[("active", C["accent2"]), ("disabled", C["bg2"])],
        foreground=[("disabled", C["muted"])],
        bordercolor=[("active", C["accent2"])],
    )

    style.configure(
        "TEntry",
        fieldbackground=C["panel"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        insertcolor=C["fg"],
        padding=4,
    )
    style.map("TEntry", bordercolor=[("focus", C["accent"])])

    # Comboboxes (+ their drop-down listbox via option db).
    style.configure(
        "TCombobox",
        fieldbackground=C["panel"],
        background=C["panel"],
        foreground=C["fg"],
        arrowcolor=C["muted"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        selectbackground=C["sel"],
        selectforeground=C["fg"],
        padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", C["panel"]), ("disabled", C["bg2"])],
        foreground=[("disabled", C["muted"])],
        arrowcolor=[("disabled", C["muted"]), ("active", C["accent"])],
    )
    root.option_add("*TCombobox*Listbox.background", C["panel"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["sel"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["fg"])
    root.option_add("*TCombobox*Listbox.font", FONT)

    # Treeview — monospaced rows so the bars line up perfectly.
    style.configure(
        "Treeview",
        background=C["panel"],
        fieldbackground=C["panel"],
        foreground=C["fg"],
        # Tall enough for the row font at this DPI; 24 px at 100%.
        rowheight=max(px(24), tkfont.nametofont(FONT_MONO, root=root).metrics("linespace") + 8),
        font=FONT_MONO,
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
    )
    style.configure(
        "Treeview.Heading",
        background=C["bg2"],
        foreground=C["fg"],
        relief="flat",
        font=FONT_BOLD,
        padding=(6, 6),
    )
    style.map(
        "Treeview.Heading", background=[("active", C["bg2"])], foreground=[("active", C["accent"])]
    )
    style.map(
        "Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["accent2"])]
    )

    # Notebook (tabs) and LabelFrame — previously unstyled, so they fell back
    # to 'clam''s own grey defaults once theme_use("clam") was set app-wide.
    style.configure(
        "TNotebook",
        background=C["bg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
    )
    style.configure(
        "TNotebook.Tab",
        background=C["bg2"],
        foreground=C["muted"],
        padding=(12, 6),
        font=FONT,
        bordercolor=C["border"],
        lightcolor=C["bg2"],
        darkcolor=C["bg2"],
    )
    style.map(
        "TNotebook.Tab", background=[("selected", C["panel"])], foreground=[("selected", C["fg"])]
    )

    style.configure(
        "TLabelframe",
        background=C["bg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
    )
    style.configure("TLabelframe.Label", background=C["bg"], foreground=C["muted"], font=FONT_BOLD)

    # Scrollbars.
    for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(
            orient,
            background=C["bg2"],
            troughcolor=C["bg"],
            bordercolor=C["bg"],
            lightcolor=C["bg2"],
            darkcolor=C["bg2"],
            arrowcolor=C["muted"],
            arrowsize=px(14),
            relief="flat",
        )
        style.map(orient, background=[("active", C["accent"])])

    # Check boxes and radio buttons (Changed folders only, dialogs).
    for kind in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            kind,
            background=C["bg"],
            foreground=C["fg"],
            indicatorbackground=C["panel"],
            indicatorforeground=C["accent"],
            font=FONT,
        )
        style.map(
            kind,
            background=[("active", C["bg"])],
            foreground=[("disabled", C["muted"])],
            indicatorbackground=[("disabled", C["bg2"]), ("pressed", C["sel"])],
        )

    # Progressbar.
    style.configure(
        "TProgressbar",
        background=C["accent"],
        troughcolor=C["bg2"],
        bordercolor=C["border"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
    )
