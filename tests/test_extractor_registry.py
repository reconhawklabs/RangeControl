from pathlib import Path

import pytest

from rangecontrol.ingest.extractors.registry import EXTRACTOR_VERSION, cache_version, extractor_for
from rangecontrol.ingest.extractors.text import extract_fallback, extract_text
from tests.support.stub_provider import StubProvider

# Scripts, configs and markup used to fall through to the printable-run
# scraper, which drops every non-ASCII byte and all structure. They are text.
ROUTING = {
    "notes.txt": "text",
    "README.md": "text",
    "rules.conf": "text",
    "data.json": "text",
    "hosts.yaml": "text",
    "config.toml": "text",
    "harden.ps1": "text",
    "deploy.sh": "text",
    "scorer.py": "text",
    "portal.html": "text",
    "scan.nmap": "text",
    "plan.pdf": "pdf",
    "msel.xlsx": "spreadsheet",
    "inventory.csv": "spreadsheet",
    "playbook.docx": "document",
    "brief.pptx": "presentation",
    "diagram.png": "image",
    "shot.JPG": "image",
    "mystery.bin": "unknown",
}


def test_routes_every_file_type_to_its_extractor_case_insensitively():
    routed = {}
    for name in ROUTING:
        kind, fn = extractor_for(Path(name))
        assert callable(fn), name
        routed[name] = kind
    assert routed == ROUTING


def test_cache_version_is_per_kind():
    """Bumping the PDF extractor must not throw away every paid-for image
    description. Each kind carries its own version in the cache key."""
    assert EXTRACTOR_VERSION
    assert cache_version("text") == f"{EXTRACTOR_VERSION}-text"
    assert cache_version("pdf").endswith("-pdf")
    assert cache_version("pdf") != cache_version("text")


def test_extract_text_decodes_real_text_and_tolerates_a_stray_byte(tmp_path):
    utf8 = tmp_path / "utf8.txt"
    utf8.write_text("firewall baseline", encoding="utf-8")
    assert extract_text(utf8, StubProvider()) == "firewall baseline"

    utf16 = tmp_path / "utf16.txt"
    utf16.write_bytes("segment notes — baseline".encode("utf-16"))
    assert extract_text(utf16, StubProvider()) == "segment notes — baseline"

    # One bad byte in a large body of real text is minor corruption, not
    # binary junk: the valid multi-byte text survives without mojibake and
    # only the bad byte is replaced.
    stray = tmp_path / "stray.txt"
    stray.write_bytes(("Réseau café — DC-01 reaches SVC-02. " * 20).encode("utf-8") + b"\xff")
    result = extract_text(stray, StubProvider())
    assert "Réseau café" in result
    assert "Ã©" not in result
    assert result.count("�") == 1


def test_extract_text_raises_on_genuinely_binary_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(bytes(range(256)))
    with pytest.raises(ValueError):
        extract_text(path, StubProvider())


def test_fallback_keeps_readable_runs_and_raises_when_there_are_none(tmp_path):
    readable = tmp_path / "a.bin"
    readable.write_bytes(b"\x00\x01SEGMENT-ALPHA\x00\x02")
    assert "SEGMENT-ALPHA" in extract_fallback(readable, StubProvider())

    junk = tmp_path / "b.bin"
    junk.write_bytes(bytes(range(1, 20)))
    with pytest.raises(ValueError):
        extract_fallback(junk, StubProvider())
