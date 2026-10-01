import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from storage_scanner import csv_to_parquet

pyarrow = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq


def _write_csv(path, rows=(("a", "1"), ("b", "2"))):
    path.write_text("name,value\n" + "\n".join(",".join(r) for r in rows) + "\n")


def test_pyarrow_available_is_true_when_the_package_is_installed():
    assert csv_to_parquet.pyarrow_available() is True


def test_default_output_path_swaps_extension_for_parquet(tmp_path):
    csv_path = tmp_path / "report.csv"
    assert csv_to_parquet.default_output_path(str(csv_path)) == str(tmp_path / "report.parquet")


def test_default_output_path_avoids_collisions(tmp_path):
    csv_path = tmp_path / "report.csv"
    (tmp_path / "report.parquet").write_text("placeholder")

    assert csv_to_parquet.default_output_path(str(csv_path)) == str(tmp_path / "report (2).parquet")


def test_compress_csv_to_parquet_round_trips_the_data(tmp_path):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path, rows=(("alpha", "1"), ("beta", "2")))

    result = csv_to_parquet.compress_csv_to_parquet(str(csv_path))

    assert result.success is True
    assert result.error is None
    assert result.output_path == str(tmp_path / "report.parquet")
    assert os.path.exists(result.output_path)

    table = pq.read_table(result.output_path)
    assert table.column_names == ["name", "value"]
    assert table.column("name").to_pylist() == ["alpha", "beta"]
    assert table.column("value").to_pylist() == [1, 2]


def test_compress_csv_to_parquet_uses_snappy_compression_by_default(tmp_path):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)

    result = csv_to_parquet.compress_csv_to_parquet(str(csv_path))

    metadata = pq.ParquetFile(result.output_path).metadata
    codec = metadata.row_group(0).column(0).compression
    assert codec.upper() == "SNAPPY"


def test_compress_csv_to_parquet_honors_an_explicit_output_path(tmp_path):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    output_path = tmp_path / "custom.parquet"

    result = csv_to_parquet.compress_csv_to_parquet(str(csv_path), output_path=str(output_path))

    assert result.output_path == str(output_path)
    assert output_path.exists()


def test_compress_csv_to_parquet_reports_a_missing_input_file(tmp_path):
    missing = tmp_path / "does_not_exist.csv"

    result = csv_to_parquet.compress_csv_to_parquet(str(missing))

    assert result.success is False
    assert result.output_path is None
    assert result.error is not None
    assert not os.path.exists(tmp_path / "does_not_exist.parquet")


def test_compress_csv_to_parquet_reports_pyarrow_missing(tmp_path, monkeypatch):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    monkeypatch.setattr(csv_to_parquet, "pv", None)

    result = csv_to_parquet.compress_csv_to_parquet(str(csv_path))

    assert result.success is False
    assert result.output_path is None
    assert "pip install pyarrow" in result.error
    assert not os.path.exists(tmp_path / "report.parquet")


def test_compress_csv_to_parquet_cleans_up_a_partial_file_on_write_failure(tmp_path, monkeypatch):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    output_path = tmp_path / "report.parquet"

    def boom(table, where, compression=None):
        Path(where).write_text("partial garbage")
        raise OSError("disk full")

    monkeypatch.setattr(csv_to_parquet.pq, "write_table", boom)

    result = csv_to_parquet.compress_csv_to_parquet(str(csv_path), output_path=str(output_path))

    assert result.success is False
    assert not output_path.exists(), "partial/broken parquet file must not be left behind"
