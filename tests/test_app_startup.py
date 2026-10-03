"""Building the real main window (StorageScannerApp) from saved settings
that change how it's laid out."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smoke_test_build import _isolated_env

# In a process of its own: history.py fixes its data folder when it's
# imported, and a second Tk interpreter in the test process is unreliable
# (tests/test_main_tree_rows.py). Prints how many panes the main window
# has (the tree, and the treemap unless it's hidden) and the menu's tick.
_START_WITH_TREEMAP_HIDDEN = """
import history
history.init_history_db()
history.set_app_metadata("show_treemap", "0")
from tkinter import Tk
from storage_scanner.app import StorageScannerApp
root = Tk()
root.withdraw()
app = StorageScannerApp(root)
print(len(app.main_panes.panes()), app.show_treemap_var.get())
root.destroy()
"""


def test_the_app_starts_with_the_treemap_turned_off(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _START_WITH_TREEMAP_HIDDEN],
        cwd=ROOT,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1", "False"]
