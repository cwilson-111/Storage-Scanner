"""Tools ▸ Data Tools: compress a CSV to Parquet, convert a CSV to Excel.

The menu (and each item) appears only when its package is installed:
pyarrow for Parquet, openpyxl for Excel. The standard download has
neither; the Data build bundles both (requirements-data.txt). Availability
is checked without importing them (find_spec), and the converters
(storage_scanner/csv_to_parquet.py, csv_to_xlsx.py) are imported only when
used, so pyarrow's tens of megabytes never load at startup.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import importlib.util
import os
from tkinter import Menu, filedialog, messagebox

from storage_scanner.formatting import human_size


def available_tools():
    """{"parquet": bool, "xlsx": bool}: which converters can run here."""
    return {
        "parquet": importlib.util.find_spec("pyarrow") is not None,
        "xlsx": importlib.util.find_spec("openpyxl") is not None,
    }


def _ask_paths(title, default_output, extension, label):
    """(csv path, output path) from two file dialogs, or None if cancelled."""
    csv_path = filedialog.askopenfilename(
        title=title, filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    if not csv_path:
        return None
    output_path = filedialog.asksaveasfilename(
        title=f"Save {label} file as",
        initialdir=os.path.dirname(csv_path),
        initialfile=os.path.basename(default_output(csv_path)),
        defaultextension=extension,
        filetypes=[(f"{label} files", f"*{extension}")],
    )
    return (csv_path, output_path) if output_path else None


class DataToolsMixin:
    def _add_data_tools_menu(self, tools_menu):
        """Add Tools ▸ Data Tools with whichever converters are installed
        (nothing at all when neither is). Works on any CSV on disk, so it's
        usable without a scan."""
        tools = available_tools()
        if not any(tools.values()):
            return
        data_menu = Menu(tools_menu, tearoff=0)
        if tools["parquet"]:
            data_menu.add_command(
                label="Compress CSV to Parquet…", command=self.compress_csv_to_parquet
            )
        if tools["xlsx"]:
            data_menu.add_command(
                label="Convert CSV to Excel (.xlsx)…", command=self.convert_csv_to_xlsx
            )
        tools_menu.add_cascade(label="Data Tools", menu=data_menu)

    def compress_csv_to_parquet(self):
        from storage_scanner.csv_to_parquet import compress_csv_to_parquet, default_output_path

        paths = _ask_paths(
            "Choose a CSV file to compress", default_output_path, ".parquet", "Parquet"
        )
        if paths is None:
            return
        csv_path, output_path = paths
        result = compress_csv_to_parquet(csv_path, output_path)
        if not result.success:
            error = f"Could not compress to Parquet:\n{result.error}"
            messagebox.showerror("Storage Scanner", error)
            return
        try:
            before = os.path.getsize(csv_path)
            after = os.path.getsize(result.output_path)
            saved = f"\n\n{human_size(before)} → {human_size(after)}" if before else ""
        except OSError:
            saved = ""
        messagebox.showinfo("Storage Scanner", f"Compressed to:\n{result.output_path}{saved}")

    def convert_csv_to_xlsx(self):
        from storage_scanner.csv_to_xlsx import convert_csv_to_xlsx, default_output_path

        paths = _ask_paths("Choose a CSV file to convert", default_output_path, ".xlsx", "Excel")
        if paths is None:
            return
        csv_path, output_path = paths
        result = convert_csv_to_xlsx(csv_path, output_path)
        if not result.success:
            messagebox.showerror("Storage Scanner", f"Could not convert to Excel:\n{result.error}")
            return
        messagebox.showinfo("Storage Scanner", f"Converted to:\n{result.output_path}")
