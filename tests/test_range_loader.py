import pytest

from rangecontrol.range_doc.loader import (
    REQUIRED_SECTIONS,
    load_range_md,
    load_template,
    missing_sections,
)


def test_returns_none_when_absent(tmp_path):
    assert load_range_md(tmp_path) is None


def test_reads_existing_file(tmp_path):
    (tmp_path / "Range.md").write_text("# Range\ncontent", encoding="utf-8")
    assert "content" in load_range_md(tmp_path)


def test_empty_file_is_treated_as_absent(tmp_path):
    (tmp_path / "Range.md").write_text("   \n", encoding="utf-8")
    assert load_range_md(tmp_path) is None


def test_template_ships_with_the_package():
    template = load_template()
    assert template.strip()
    for section in REQUIRED_SECTIONS:
        assert section in template


def test_template_includes_the_protected_dependency_index():
    assert "Protected Dependency Index" in load_template()


def test_required_sections_are_declared():
    assert "Required Access Paths" in REQUIRED_SECTIONS
    assert "MSEL & Inject Catalog" in REQUIRED_SECTIONS
    assert "Protected Dependency Index" in REQUIRED_SECTIONS
    assert "Ingest Gaps" in REQUIRED_SECTIONS


def test_missing_sections_reports_gaps():
    assert "Ingest Gaps" in missing_sections("## Exercise Overview\n")


def test_prose_mention_does_not_satisfy_a_missing_section():
    present = [n for n in REQUIRED_SECTIONS if n != "Automation & Scripts"]
    body = "\n".join(f"## {n}\n\ncontent\n" for n in present)
    body += "\nAutomation & Scripts could not be determined from the source material.\n"
    assert missing_sections(body) == ("Automation & Scripts",)


def test_headings_at_any_level_satisfy_a_section():
    assert "Ingest Gaps" not in missing_sections("### Ingest Gaps\n")


def test_complete_document_reports_no_gaps():
    body = "\n".join(f"## {name}\n" for name in REQUIRED_SECTIONS)
    assert missing_sections(body) == ()


def test_template_loads_from_package_data():
    """Must not depend on the repo layout: a frozen binary has no repo root."""
    from rangecontrol.range_doc.loader import load_template

    content = load_template()
    assert "## Exercise Overview" in content


def test_template_does_not_resolve_through_parent_directories(monkeypatch, tmp_path):
    """Simulates a frozen layout where the repo root is absent.

    importlib.resources reads relative to the package, so moving the process
    out of the source tree must make no difference.
    """
    monkeypatch.chdir(tmp_path)
    from rangecontrol.range_doc.loader import load_template

    assert "## Exercise Overview" in load_template()
