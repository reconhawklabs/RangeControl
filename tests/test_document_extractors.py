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


def test_a_password_protected_office_file_says_so(tmp_path):
    """An encrypted .docx is an OLE container around an EncryptedPackage
    stream. python-docx reports PackageNotFoundError, which tells an
    operator nothing about the fix (remove the password, save again)."""
    path = tmp_path / "gobook.docx"
    path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 512)
    with pytest.raises(ValueError, match="password"):
        extract_docx(path, StubProvider())


def test_a_password_protected_pdf_says_so(tmp_path):
    pypdf = pytest.importorskip("pypdf")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    path = tmp_path / "locked.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(ValueError, match="password"):
        extract_pdf(path, StubProvider())


def _pdf_with_text(path, text):
    """A minimal single-page PDF whose only content is ``text``."""
    stream = f"BT /F1 12 Tf 40 150 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def test_an_irm_protected_pdf_placeholder_is_not_mistaken_for_content(tmp_path):
    """Observed on a real range cheat sheet: the one readable page says the
    file is protected by Microsoft Office, and that sentence was ingested
    as the document."""
    path = _pdf_with_text(
        tmp_path / "cheat.pdf",
        "This PDF Document has been protected. The reader you are using does "
        "not support opening files protected by Microsoft Office",
    )
    with pytest.raises(ValueError, match="Rights Management"):
        extract_pdf(path, StubProvider())


def test_an_ordinary_one_page_pdf_still_extracts(tmp_path):
    path = _pdf_with_text(tmp_path / "plain.pdf", "FW-1 sits between HQ-LAN and the DMZ")
    assert "FW-1" in extract_pdf(path, StubProvider())
