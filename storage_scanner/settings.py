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
# Theme — "Structural Light": a light, data-tool palette. Fine hairlines and
# one confident blue instead of neon glow — see After Jarvis (the palette
# comparison put together while choosing this) for the reasoning.
# --------------------------------------------------------------------------- #

COLORS = {
    "bg":      "#f6f7f9",   # window background
    "bg2":     "#eef1f6",   # header/heading chrome
    "panel":   "#ffffff",   # content surfaces (tree rows, cards)
    "border":  "#e3e6eb",
    "fg":      "#1a1d23",   # primary text
    "muted":   "#6b7280",   # secondary text
    "accent":  "#3454d1",   # primary interactive blue
    "accent2": "#22398f",   # deeper navy — emphasis (links, keeper rows)
    "sel":     "#dbe4f7",   # selected-row background
    "warning": "#d97706",   # caution: review candidates, growth, mid-heat
    "good":    "#1a8754",   # positive: shrinking / improvement
    "error":   "#c0331f",   # critical: errors, top-heat, spikes
    "stripe":  "#fafbfc",   # subtle zebra striping
}

# Tkinter can only use fonts actually installed on the OS — there's no
# @font-face equivalent — so this picks each platform's native modern UI
# font rather than hardcoding one name and silently falling back on
# whichever platform doesn't have it.
if IS_MACOS:
    FONT = ("Helvetica Neue", 12)
    FONT_BOLD = ("Helvetica Neue", 12, "bold")
    FONT_MONO = ("Menlo", 11)
    FONT_MONO_BOLD = ("Menlo", 11, "bold")
else:
    FONT = ("Segoe UI", 9)
    FONT_BOLD = ("Segoe UI Semibold", 9)
    FONT_MONO = ("Consolas", 10)
    FONT_MONO_BOLD = ("Consolas", 10, "bold")


def heat_color(fraction):
    """Map 0..1 to a calm→amber→red heat gradient (big hogs run hot).

    A semantic scale, deliberately not built from the UI's own accent blue
    (that's reserved for actions/chrome) — small items stay a quiet
    neutral, and only real space hogs earn the warning/critical colors.
    """
    f = max(0.0, min(1.0, fraction))
    neutral, warning, critical = (0x9a, 0xa1, 0xb0), (0xd9, 0x77, 0x06), (0xc0, 0x33, 0x1f)
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


def contrast_text_color(hex_color):
    """Black-ish or white text, whichever reads better against
    `hex_color` — computed from relative luminance rather than guessed
    per-case, so it stays correct if the heat/fill colors above change."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return COLORS["fg"] if luminance > 0.55 else "#ffffff"


def apply_theme(root):
    """Style every ttk widget with the Structural Light palette."""
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

    # Buttons — flat chips with a hairline border; the primary blue only
    # shows up on hover/press, not as a permanent fill.
    style.configure("TButton", background=C["panel"], foreground=C["fg"],
                    bordercolor=C["border"], lightcolor=C["panel"],
                    darkcolor=C["panel"], relief="flat", padding=(12, 5),
                    font=FONT)
    style.map("TButton",
              background=[("active", C["accent"]), ("disabled", C["bg2"])],
              foreground=[("active", "#ffffff"), ("disabled", C["muted"])],
              bordercolor=[("active", C["accent"])])

    # The one permanently-filled button in the whole app — reserved for the
    # single primary action (Scan), so it still means something.
    style.configure("Primary.TButton", background=C["accent"], foreground="#ffffff",
                    bordercolor=C["accent"], lightcolor=C["accent"],
                    darkcolor=C["accent"], relief="flat", padding=(12, 5),
                    font=FONT_BOLD)
    style.map("Primary.TButton",
              background=[("active", C["accent2"]), ("disabled", C["bg2"])],
              foreground=[("disabled", C["muted"])],
              bordercolor=[("active", C["accent2"])])

    style.configure("TEntry", fieldbackground=C["panel"], foreground=C["fg"],
                    bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], insertcolor=C["fg"], padding=4)
    style.map("TEntry", bordercolor=[("focus", C["accent"])])

    # Comboboxes (+ their drop-down listbox via option db).
    style.configure("TCombobox", fieldbackground=C["panel"], background=C["panel"],
                    foreground=C["fg"], arrowcolor=C["muted"],
                    bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], selectbackground=C["sel"],
                    selectforeground=C["fg"], padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", C["panel"]), ("disabled", C["bg2"])],
              foreground=[("disabled", C["muted"])],
              arrowcolor=[("disabled", C["muted"]), ("active", C["accent"])])
    root.option_add("*TCombobox*Listbox.background", C["panel"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["sel"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["fg"])
    root.option_add("*TCombobox*Listbox.font", FONT)

    # Treeview — monospaced rows so the bars line up perfectly.
    style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                    foreground=C["fg"], rowheight=24, font=FONT_MONO,
                    bordercolor=C["border"])
    style.configure("Treeview.Heading", background=C["bg2"], foreground=C["fg"],
                    relief="flat", font=FONT_BOLD, padding=(6, 6))
    style.map("Treeview.Heading",
              background=[("active", C["bg2"])],
              foreground=[("active", C["accent"])])
    style.map("Treeview",
              background=[("selected", C["sel"])],
              foreground=[("selected", C["accent2"])])

    # Notebook (tabs) and LabelFrame — previously unstyled, so they fell back
    # to 'clam''s own grey defaults once theme_use("clam") was set app-wide.
    style.configure("TNotebook", background=C["bg"], bordercolor=C["border"])
    style.configure("TNotebook.Tab", background=C["bg2"], foreground=C["muted"],
                    padding=(12, 6), font=FONT, bordercolor=C["border"])
    style.map("TNotebook.Tab",
              background=[("selected", C["panel"])],
              foreground=[("selected", C["fg"])])

    style.configure("TLabelframe", background=C["bg"], bordercolor=C["border"])
    style.configure("TLabelframe.Label", background=C["bg"], foreground=C["muted"],
                    font=FONT_BOLD)

    # Scrollbars.
    for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(orient, background=C["bg2"], troughcolor=C["bg"],
                        bordercolor=C["bg"], arrowcolor=C["muted"],
                        relief="flat")
        style.map(orient, background=[("active", C["accent"])])

    # Progressbar.
    style.configure("TProgressbar", background=C["accent"], troughcolor=C["bg2"],
                    bordercolor=C["border"], lightcolor=C["accent"],
                    darkcolor=C["accent"])
