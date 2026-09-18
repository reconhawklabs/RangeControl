from pathlib import Path

import pytest

from rangecontrol.ingest.extractors.registry import EXTRACTOR_VERSION, extractor_for
from rangecontrol.ingest.extractors.text import extract_fallback, extract_text
from tests.support.stub_provider import StubProvider


@pytest.mark.parametrize(
    "name,expected_kind",
    [
        ("notes.txt", "text"),
        ("README.md", "text"),
        ("rules.conf", "text"),
        ("data.json", "text"),
        ("hosts.yaml", "text"),
        ("plan.pdf", "pdf"),
        ("msel.xlsx", "spreadsheet"),
        ("inventory.csv", "spreadsheet"),
        ("playbook.docx", "document"),
        ("diagram.png", "image"),
        ("shot.JPG", "image"),
        ("mystery.bin", "unknown"),
    ],
)
def test_dispatches_by_extension_case_insensitively(name, expected_kind):
    kind, fn = extractor_for(Path(name))
    assert kind == expected_kind
    assert callable(fn)


def test_extractor_version_is_a_nonempty_string():
    assert isinstance(EXTRACTOR_VERSION, str)
    assert EXTRACTOR_VERSION


def test_extract_text_reads_utf8(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("firewall baseline", encoding="utf-8")
    assert extract_text(path, StubProvider()) == "firewall baseline"


def test_extract_text_tolerates_invalid_bytes(tmp_path):
    path = tmp_path / "a.txt"
    # A couple of stray bad bytes in an otherwise-large body of real text
    # keeps the replacement ratio well under the 2% cutoff, so this is
    # tolerated as a minor corruption rather than rejected as binary junk.
    path.write_bytes(b"firewall baseline notes " * 20 + b"\xff\xfe")
    assert "firewall baseline" in extract_text(path, StubProvider())


def test_extract_text_decodes_utf16_with_bom(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("segment notes — baseline".encode("utf-16"))
    assert extract_text(path, StubProvider()) == "segment notes — baseline"


def test_extract_text_raises_on_genuinely_binary_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(bytes(range(256)))
    with pytest.raises(ValueError):
        extract_text(path, StubProvider())


def test_stray_byte_preserves_valid_multibyte_text(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(("Réseau café — DC-01 reaches SVC-02. " * 20).encode("utf-8") + b"\xff")
    result = extract_text(path, StubProvider())
    assert "Réseau café" in result  # valid multi-byte text survives intact
    assert "Ã©" not in result  # and is not mojibake
    assert result.count("�") == 1  # only the genuinely bad byte is replaced


def test_fallback_extracts_readable_text_from_binary(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"\x00\x01SEGMENT-ALPHA\x00\x02")
    assert "SEGMENT-ALPHA" in extract_fallback(path, StubProvider())


def test_fallback_raises_on_content_with_no_readable_text(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(bytes(range(1, 20)))
    with pytest.raises(ValueError):
        extract_fallback(path, StubProvider())


@pytest.mark.parametrize(
    "name,expected_kind",
    [
        ("brief.pptx", "presentation"),
        ("harden.ps1", "text"),
        ("deploy.sh", "text"),
        ("scorer.py", "text"),
        ("portal.html", "text"),
        ("config.toml", "text"),
        ("scan.nmap", "text"),
    ],
)
def test_scripts_and_slides_get_real_extractors(name, expected_kind):
    """Scripts used to fall through to the printable-run scraper, which drops
    every non-ASCII byte and all structure. They are text."""
    kind, _ = extractor_for(Path(name))
    assert kind == expected_kind


def test_cache_version_is_per_kind():
    """Bumping the PDF extractor must not throw away every paid-for image
    description. Each kind carries its own version in the cache key."""
    from rangecontrol.ingest.extractors.registry import cache_version

    assert cache_version("text") == f"{EXTRACTOR_VERSION}-text"
    assert cache_version("pdf").endswith("-pdf")
    assert cache_version("pdf") != cache_version("text")
