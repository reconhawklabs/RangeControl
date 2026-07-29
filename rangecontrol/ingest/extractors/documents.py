"""PDF, Word, and spreadsheet extraction.

Every extractor raises ValueError when it produces no usable text. Callers
record that as an unreadable document rather than aborting startup.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from rangecontrol.llm.base import Provider


def extract_pdf(path: Path, provider: Provider) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - any parse failure is "unreadable"
        raise ValueError(f"could not parse PDF: {type(exc).__name__}") from exc

    sections = [
        f"--- page {number} ---\n{body.strip()}"
        for number, body in enumerate(pages, start=1)
        if body.strip()
    ]
    if not sections:
        raise ValueError("PDF contained no extractable text (likely scanned images)")
    return "\n\n".join(sections)


def extract_docx(path: Path, provider: Provider) -> str:
    import docx

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse DOCX: {type(exc).__name__}") from exc

    lines = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))

    if not lines:
        raise ValueError("DOCX contained no text")
    return "\n".join(lines)


def extract_spreadsheet(path: Path, provider: Provider) -> str:
    if path.suffix.lower() in {".csv", ".tsv"}:
        return _extract_delimited(path)
    return _extract_xlsx(path)


def _extract_delimited(path: Path) -> str:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    lines = []
    for row in rows:
        cells = [cell.strip() for cell in row]
        if any(cells):
            lines.append(" | ".join(cells))

    if not lines:
        raise ValueError("spreadsheet contained no rows")
    return "\n".join(lines)


def _extract_xlsx(path: Path) -> str:
    import openpyxl

    try:
        book = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse workbook: {type(exc).__name__}") from exc

    sections: list[str] = []
    try:
        for sheet in book.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    lines.append(" | ".join(cells))
            if lines:
                sections.append(f"--- sheet: {sheet.title} ---\n" + "\n".join(lines))
    except Exception as exc:  # noqa: BLE001 - lazy parse failure mid-iteration
        raise ValueError(f"could not read workbook rows: {type(exc).__name__}") from exc
    finally:
        book.close()

    if not sections:
        raise ValueError("workbook contained no rows")
    return "\n\n".join(sections)
