"""Converts a CSV file to an Excel .xlsx workbook via openpyxl -- lets a
report exported as CSV (scan results, history exports, etc.) be opened
directly in Excel with real numeric columns instead of everything read in
as text.

openpyxl is NOT a required runtime dependency of this app, same reasoning
as csv_to_parquet.py's pyarrow dependency -- most users of a disk-usage
tool will never touch this feature, so it's imported lazily (module-level
try/except, same convention history.py uses for matplotlib) and its
absence is reported as a normal ExcelResult failure rather than an
ImportError crashing the app.
"""

import csv
import os
from collections import namedtuple

try:
    import openpyxl
except ImportError:  # pragma: no cover - optional runtime dependency
    openpyxl = None

from storage_scanner.logging_setup import logger

ExcelResult = namedtuple(
    "ExcelResult", ["success", "output_path", "error"],
)

_INSTALL_HINT = (
    "Excel export needs the optional 'openpyxl' package, which isn't "
    "installed. Install it with: pip install openpyxl"
)

# Excel's own hard per-cell/sheet limits (rows including the header) -- fail
# with a clear message instead of a confusing exception deep in openpyxl.
_MAX_ROWS = 1_048_576
_MAX_COLUMNS = 16_384


def openpyxl_available():
    return openpyxl is not None


def default_output_path(csv_path):
    """`csv_path` with its extension replaced by ".xlsx", or with
    " (2).xlsx" etc. appended if that path is already taken -- same
    collision-avoidance convention as archive.py's _unique_archive_path."""
    base = os.path.splitext(csv_path)[0]
    candidate = base + ".xlsx"
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = f"{base} ({n}).xlsx"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _coerce(value):
    """Give Excel real numeric cells instead of text wherever a value
    round-trips cleanly through int/float -- everything else (including
    values with leading zeros, like "007", which int() would silently
    strip) stays exactly as written."""
    if value == "":
        return value
    try:
        as_int = int(value)
    except ValueError:
        pass
    else:
        # Only a real integer if it round-trips -- "007" would silently
        # become 7 (int) or 7.0 (float) otherwise, changing an identifier
        # into a different value. A value that fails this check is treated
        # as text, not handed to float() as a fallback.
        return as_int if str(as_int) == value else value
    try:
        return float(value)
    except ValueError:
        return value


def convert_csv_to_xlsx(csv_path, output_path=None, sheet_name="Sheet1"):
    """Read `csv_path` and write it out as a single-sheet .xlsx workbook.
    Returns an ExcelResult; never raises -- any failure (missing openpyxl,
    unreadable CSV, a sheet too large for Excel's own row/column limits,
    write failure) comes back as `success=False, error=<message>`.
    """
    if not openpyxl_available():
        return ExcelResult(False, None, _INSTALL_HINT)

    if output_path is None:
        output_path = default_output_path(csv_path)

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))

        if rows and len(rows[0]) > _MAX_COLUMNS:
            raise ValueError(
                f"CSV has {len(rows[0]):,} columns, more than Excel's "
                f"{_MAX_COLUMNS:,}-column limit"
            )
        if len(rows) > _MAX_ROWS:
            raise ValueError(
                f"CSV has {len(rows):,} rows, more than Excel's "
                f"{_MAX_ROWS:,}-row limit"
            )

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = sheet_name
        for row in rows:
            sheet.append([_coerce(cell) for cell in row])
        workbook.save(output_path)
    except Exception as exc:  # noqa: BLE001 - openpyxl/csv raise their own exception types
        logger.exception("CSV to Excel conversion failed for %r", csv_path)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                logger.warning("Could not clean up partial xlsx file %r", output_path, exc_info=True)
        return ExcelResult(False, None, str(exc))

    return ExcelResult(True, output_path, None)
