"""Compresses a CSV file to Parquet (Snappy-compressed columnar format) via
PyArrow -- usually a large size reduction for exported reports (scan
results, history exports, etc.) that are mostly repeated values.

PyArrow is NOT a required runtime dependency of this app (see
requirements-dev.txt's own note that Storage Scanner has none besides the
optional matplotlib charting) -- it's a large package (tens of MB) that most
users of a disk-usage tool will never need, so it's imported lazily here,
only when this feature is actually used, and its absence is reported as a
normal ParquetResult failure rather than an ImportError crashing the app.
"""

import os
from collections import namedtuple

try:
    import pyarrow.csv as pv
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - optional runtime dependency
    pv = None
    pq = None

from storage_scanner.logging_setup import logger

ParquetResult = namedtuple(
    "ParquetResult", ["success", "output_path", "error"],
)

_INSTALL_HINT = (
    "Parquet compression needs the optional 'pyarrow' package, which isn't "
    "installed. Install it with: pip install pyarrow"
)


def pyarrow_available():
    return pv is not None


def default_output_path(csv_path):
    """`csv_path` with its extension replaced by ".parquet", or with
    " (2).parquet" etc. appended if that path is already taken -- same
    collision-avoidance convention as archive.py's _unique_archive_path."""
    base = os.path.splitext(csv_path)[0]
    candidate = base + ".parquet"
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = f"{base} ({n}).parquet"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def compress_csv_to_parquet(csv_path, output_path=None, compression="snappy"):
    """Read `csv_path` and write it out as a Parquet file, defaulting to
    Snappy compression. Returns a ParquetResult; never raises -- any
    failure (missing pyarrow, unreadable/malformed CSV, write failure) comes
    back as `success=False, error=<message>` for the caller to display.
    """
    if not pyarrow_available():
        return ParquetResult(False, None, _INSTALL_HINT)

    if output_path is None:
        output_path = default_output_path(csv_path)

    try:
        table = pv.read_csv(csv_path)
        pq.write_table(table, output_path, compression=compression)
    except Exception as exc:  # noqa: BLE001 - pyarrow raises its own exception types
        logger.exception("CSV to Parquet compression failed for %r", csv_path)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                logger.warning("Could not clean up partial Parquet file %r", output_path, exc_info=True)
        return ParquetResult(False, None, str(exc))

    return ParquetResult(True, output_path, None)
