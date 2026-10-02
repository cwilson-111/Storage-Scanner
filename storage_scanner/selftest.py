"""`--selftest-gui`: build the real main window, hidden, and close it.

smoke_test_build.py runs this against every packaged build. The `--cli`
check alone never creates a Tk window, so a build with a broken Tcl/Tk
bundle, a theme font or image it can't load, or a UI module PyInstaller left
out would still pass it. This builds the whole StorageScannerApp (every
window mixin, the toolbar and menus, the theme and the icon), lets Tk run
its event loop for a moment, then destroys it.

Run it with the app's data folder pointed somewhere disposable (the smoke
test does): building the app opens the scan-history database, creating it
on a first run.
"""

import sys
import traceback

RUN_SECONDS = 1.0


def run_selftest_gui(seconds=RUN_SECONDS):
    """0 when the main window was built, ran and closed without an error,
    else 1. Errors go to stderr when there is one (a windowed build has
    none, so the exit code is what counts)."""
    from tkinter import Tk

    from storage_scanner import appearance
    from storage_scanner.app import StorageScannerApp

    errors = []

    def report(*exc_info):
        errors.append(exc_info)
        root.destroy()

    try:
        appearance.enable_dpi_awareness()  # as main() does
        root = Tk()
        root.withdraw()
        StorageScannerApp(root)
        # The app's own handler opens an error dialog, which would wait for
        # a click that never comes; a self-test records the error and stops.
        root.report_callback_exception = report
        root.after(int(seconds * 1000), root.destroy)
        root.mainloop()
    except Exception:  # noqa: BLE001 - any failure is the answer
        errors.append(sys.exc_info())

    for exc_info in errors:
        if sys.stderr is not None:
            traceback.print_exception(*exc_info, file=sys.stderr)
    if sys.stdout is not None:
        print("GUI self-test " + ("failed" if errors else "passed"))
    return 1 if errors else 0
