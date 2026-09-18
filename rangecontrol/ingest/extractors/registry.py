"""Extension-to-extractor dispatch and per-kind cache versions.

Bump the version for a kind in KIND_VERSIONS whenever that extractor's output
changes; it is part of the cache key, so a bump forces re-extraction of every
file of that kind and nothing else. Image descriptions cost an API call each,
which is why the versions are per kind rather than one global number.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rangecontrol.ingest.extractors import documents, image, text
from rangecontrol.llm.base import Provider

# The base version, used by every kind without an entry in KIND_VERSIONS.
EXTRACTOR_VERSION = "1"

KIND_VERSIONS: dict[str, str] = {
    # 2: layout-preserving text plus embedded-image descriptions.
    "pdf": "2",
    # 2: headers, footers, and embedded-image descriptions.
    "document": "2",
}

Extractor = Callable[[Path, Provider], str]

# Anything a person would open in a text editor: notes, configs, exports,
# and the scripts an exercise runs. Scripts in particular used to fall
# through to the printable-run scraper, which drops every non-ASCII byte and
# all structure.
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".conf", ".cfg", ".ini", ".properties", ".env", ".log", ".xml", ".rules",
    ".html", ".htm", ".sql", ".nmap", ".gnmap", ".lst", ".diff", ".patch",
    ".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".bat", ".cmd", ".vbs",
    ".reg", ".py", ".rb", ".pl", ".js", ".ts", ".go", ".rs", ".c", ".h",
    ".java", ".cs", ".php", ".tf", ".hcl", ".j2", ".jinja", ".jinja2",
}
_SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv"}
_DOCUMENT_SUFFIXES = {".docx"}
_PRESENTATION_SUFFIXES = {".pptx", ".pptm"}
_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}


def cache_version(kind: str) -> str:
    """The cache-key version for ``kind``."""
    return f"{KIND_VERSIONS.get(kind, EXTRACTOR_VERSION)}-{kind}"


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
    if suffix in _PRESENTATION_SUFFIXES:
        return "presentation", documents.extract_pptx
    if suffix in _IMAGE_SUFFIXES:
        return "image", image.extract_image
    return "unknown", text.extract_fallback
