import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from storage_scanner import csv_to_xlsx

openpyxl = pytest.importorskip("openpyxl")


def _write_csv(path, rows=(("a", "1"), ("b", "2"))):
    path.write_text("name,value\n" + "\n".join(",".join(r) for r in rows) + "\n")


def test_openpyxl_available_is_true_when_the_package_is_installed():
    assert csv_to_xlsx.openpyxl_available() is True


def test_default_output_path_swaps_extension_for_xlsx(tmp_path):
    csv_path = tmp_path / "report.csv"
    assert csv_to_xlsx.default_output_path(str(csv_path)) == str(tmp_path / "report.xlsx")


def test_default_output_path_avoids_collisions(tmp_path):
    csv_path = tmp_path / "report.csv"
    (tmp_path / "report.xlsx").write_text("placeholder")

    assert csv_to_xlsx.default_output_path(str(csv_path)) == str(tmp_path / "report (2).xlsx")


def test_convert_csv_to_xlsx_round_trips_the_data(tmp_path):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path, rows=(("alpha", "1"), ("beta", "2")))

    result = csv_to_xlsx.convert_csv_to_xlsx(str(csv_path))

    assert result.success is True
    assert result.error is None
    assert result.output_path == str(tmp_path / "report.xlsx")
    assert os.path.exists(result.output_path)

    workbook = openpyxl.load_workbook(result.output_path)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows == [("name", "value"), ("alpha", 1), ("beta", 2)]


def test_convert_csv_to_xlsx_keeps_leading_zeros_as_text(tmp_path):
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("code,value\n007,42\n")

    result = csv_to_xlsx.convert_csv_to_xlsx(str(csv_path))

    workbook = openpyxl.load_workbook(result.output_path)
    rows = list(workbook.active.iter_rows(values_only=True))
    assert rows == [("code", "value"), ("007", 42)]


def test_convert_csv_to_xlsx_honors_an_explicit_output_path_and_sheet_name(tmp_path):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    output_path = tmp_path / "custom.xlsx"

    result = csv_to_xlsx.convert_csv_to_xlsx(
        str(csv_path), output_path=str(output_path), sheet_name="Export"
    )

    assert result.output_path == str(output_path)
    workbook = openpyxl.load_workbook(result.output_path)
    assert workbook.active.title == "Export"


def test_convert_csv_to_xlsx_reports_a_missing_input_file(tmp_path):
    missing = tmp_path / "does_not_exist.csv"

    result = csv_to_xlsx.convert_csv_to_xlsx(str(missing))

    assert result.success is False
    assert result.output_path is None
    assert not os.path.exists(tmp_path / "does_not_exist.xlsx")


def test_convert_csv_to_xlsx_reports_openpyxl_missing(tmp_path, monkeypatch):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    monkeypatch.setattr(csv_to_xlsx, "openpyxl", None)

    result = csv_to_xlsx.convert_csv_to_xlsx(str(csv_path))

    assert result.success is False
    assert result.output_path is None
    assert "pip install openpyxl" in result.error
    assert not os.path.exists(tmp_path / "report.xlsx")


def test_convert_csv_to_xlsx_cleans_up_a_partial_file_on_write_failure(tmp_path, monkeypatch):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    output_path = tmp_path / "report.xlsx"

    def boom(self, where):
        Path(where).write_text("partial garbage")
        raise OSError("disk full")

    monkeypatch.setattr(csv_to_xlsx.openpyxl.Workbook, "save", boom)

    result = csv_to_xlsx.convert_csv_to_xlsx(str(csv_path), output_path=str(output_path))

    assert result.success is False
    assert not output_path.exists(), "partial/broken xlsx file must not be left behind"


def test_convert_csv_to_xlsx_rejects_a_sheet_over_excels_column_limit(tmp_path, monkeypatch):
    csv_path = tmp_path / "report.csv"
    _write_csv(csv_path)
    monkeypatch.setattr(csv_to_xlsx, "_MAX_COLUMNS", 1)

    result = csv_to_xlsx.convert_csv_to_xlsx(str(csv_path))

    assert result.success is False
    assert "column limit" in result.error
