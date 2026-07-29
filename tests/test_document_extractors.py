import csv

import pytest

from rangecontrol.ingest.extractors.documents import (
    extract_docx,
    extract_pdf,
    extract_spreadsheet,
)
from tests.support.stub_provider import StubProvider


def test_extracts_csv_rows(tmp_path):
    path = tmp_path / "hosts.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["host", "role"])
        writer.writerow(["DC-01", "domain controller"])
    result = extract_spreadsheet(path, StubProvider())
    assert "host" in result
    assert "DC-01" in result
    assert "domain controller" in result


def test_extracts_xlsx_with_sheet_names(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "msel.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Injects"
    sheet.append(["ID", "Description"])
    sheet.append(["MSEL-01", "initial foothold"])
    book.save(path)
    result = extract_spreadsheet(path, StubProvider())
    assert "Injects" in result
    assert "MSEL-01" in result
    assert "initial foothold" in result


def test_extracts_docx_paragraphs(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "playbook.docx"
    document = docx.Document()
    document.add_heading("Response Playbook", level=1)
    document.add_paragraph("Escalate to range control before blocking.")
    document.save(path)
    result = extract_docx(path, StubProvider())
    assert "Response Playbook" in result
    assert "Escalate to range control" in result


def test_extracts_pdf_pages_with_markers(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "empty.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    # A blank PDF has no extractable text, so extraction must fail loudly.
    with pytest.raises(ValueError):
        extract_pdf(path, StubProvider())


def test_corrupt_pdf_raises_value_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not really a pdf")
    with pytest.raises(ValueError):
        extract_pdf(path, StubProvider())


def test_whitespace_only_csv_raises_value_error(tmp_path):
    path = tmp_path / "blank.csv"
    path.write_text("  ,  ,  \n \t , ,  \n", encoding="utf-8")
    with pytest.raises(ValueError):
        extract_spreadsheet(path, StubProvider())


def test_whitespace_only_xlsx_raises_value_error(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "blank.xlsx"
    book = openpyxl.Workbook()
    book.active.append(["  ", " "])
    book.save(path)
    with pytest.raises(ValueError):
        extract_spreadsheet(path, StubProvider())


def test_empty_csv_raises_value_error(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        extract_spreadsheet(path, StubProvider())
