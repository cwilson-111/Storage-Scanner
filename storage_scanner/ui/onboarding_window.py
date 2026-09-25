"""Getting Started window: the first-run "Before you start" guide.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Shown
once automatically, shortly after the main window first comes up, and
reopenable any time from Help ▸ Getting Started…. What it says and whether
it's due are decided in storage_scanner/onboarding.py (no Tk there); this
file only lays it out. Closing it any way at all (OK, Escape, the window's
close button) marks it seen.
"""

from tkinter import BOTTOM, LEFT, RIGHT, TOP, TclError, Toplevel, X, ttk

from storage_scanner.logging_setup import logger
from storage_scanner.onboarding import (
    mark_onboarding_seen,
    onboarding_sections,
    should_show_onboarding,
)
from storage_scanner.platform_support import resource_path
from storage_scanner.settings import COLORS

_WRAP = 520


class OnboardingMixin:
    def _show_onboarding_on_launch(self):
        if should_show_onboarding():
            self.show_onboarding()

    def show_onboarding(self):
        existing = getattr(self, "_onboarding_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_set()
            return

        win = Toplevel(self.root)
        self._onboarding_win = win
        win.withdraw()  # position before showing, so it doesn't flash at 0,0
        win.configure(bg=COLORS["bg"])
        win.title("Before You Start")
        win.resizable(False, False)
        win.transient(self.root)
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Getting Started window iconbitmap failed", exc_info=True)

        def close(_event=None):
            mark_onboarding_seen()
            self._onboarding_win = None
            win.destroy()

        body = ttk.Frame(win, padding=(18, 14, 18, 6))
        body.pack(side=TOP, fill=X)

        ttk.Label(
            body,
            text="A few things worth knowing before you clean anything up.",
            wraplength=_WRAP,
        ).pack(side=TOP, anchor="w", pady=(0, 6))

        for heading, text in onboarding_sections():
            ttk.Label(body, text=heading, style="Accent.TLabel").pack(
                side=TOP, anchor="w", pady=(8, 2)
            )
            ttk.Label(body, text=text, wraplength=_WRAP, justify=LEFT).pack(side=TOP, anchor="w")

        ttk.Label(
            body,
            text="Reopen this any time from Help ▸ Getting Started…",
            foreground=COLORS["muted"],
            wraplength=_WRAP,
        ).pack(side=TOP, anchor="w", pady=(12, 0))

        button_bar = ttk.Frame(win, padding=(18, 8, 18, 14))
        button_bar.pack(side=BOTTOM, fill=X)
        ok_btn = ttk.Button(button_bar, text="OK", command=close)
        ok_btn.pack(side=RIGHT)

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", close)
        win.bind("<Return>", close)

        self._center_over_root(win)
        win.deiconify()
        ok_btn.focus_set()
        try:
            win.grab_set()
        except TclError:  # not viewable yet on some window managers; still usable
            logger.debug("Getting Started window grab_set failed", exc_info=True)

    def _center_over_root(self, win):
        win.update_idletasks()
        width, height = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
