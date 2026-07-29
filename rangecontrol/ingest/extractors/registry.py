"""Extension-to-extractor dispatch.

Bump EXTRACTOR_VERSION whenever an extractor's output changes; it is part of
the cache key, so a bump forces re-extraction of every affected file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rangecontrol.ingest.extractors import documents, image, text
from rangecontrol.llm.base import Provider

EXTRACTOR_VERSION = "1"

Extractor = Callable[[Path, Provider], str]

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml",
    ".conf", ".cfg", ".ini", ".log", ".xml", ".rules",
}
_SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv"}
_DOCUMENT_SUFFIXES = {".docx"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def extractor_for(path: Path) -> tuple[str, Extractor]:
    """Return the ``(kind, extractor)`` pair for a resource file."""
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return "text", text.extract_text
    if suffix == ".pdf":
        return "pdf", documents.extract_pdf
    if suffix in _SPREADSHEET_SUFFIXES:
        return "spreadsheet", documents.extract_spreadsheet
    if suffix in _DOCUMENT_SUFFIXES:
        return "document", documents.extract_docx
    if suffix in _IMAGE_SUFFIXES:
        return "image", image.extract_image
    return "unknown", text.extract_fallback
