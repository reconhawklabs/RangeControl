from rangecontrol.range_doc.loader import (
    REQUIRED_SECTIONS,
    load_range_md,
    load_template,
    missing_sections,
)


def test_a_blank_or_absent_range_md_loads_as_none(tmp_path):
    assert load_range_md(tmp_path) is None
    (tmp_path / "Range.md").write_text("   \n", encoding="utf-8")
    assert load_range_md(tmp_path) is None
    (tmp_path / "Range.md").write_text("# Range\ncontent", encoding="utf-8")
    assert "content" in load_range_md(tmp_path)


def test_template_ships_as_package_data_with_every_required_section(monkeypatch, tmp_path):
    """Must not depend on the repo layout: a frozen binary has no repo root,
    so loading from an unrelated working directory must make no difference."""
    monkeypatch.chdir(tmp_path)
    template = load_template()
    assert "## Exercise Overview" in template
    assert "Protected Dependency Index" in REQUIRED_SECTIONS
    for section in REQUIRED_SECTIONS:
        assert section in template


def test_missing_sections_reports_gaps_and_accepts_any_heading_level():
    assert "Ingest Gaps" in missing_sections("## Exercise Overview\n")
    assert "Ingest Gaps" not in missing_sections("### Ingest Gaps\n")
    assert missing_sections("\n".join(f"## {name}\n" for name in REQUIRED_SECTIONS)) == ()


def test_prose_mention_does_not_satisfy_a_missing_section():
    present = [n for n in REQUIRED_SECTIONS if n != "Automation & Scripts"]
    body = "\n".join(f"## {n}\n\ncontent\n" for n in present)
    body += "\nAutomation & Scripts could not be determined from the source material.\n"
    assert missing_sections(body) == ("Automation & Scripts",)
