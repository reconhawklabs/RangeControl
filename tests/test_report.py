import io

from rangecontrol.ingest.corpus import Corpus, ExtractedDoc
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from rangecontrol.range_doc.report import (
    LARGE_CONTEXT_CHARS,
    build_report,
    confirm,
    format_report,
)

RANGE_MD = """\
## MSEL & Inject Catalog
### MSEL-01 — foothold
### MSEL-02 — escalation
## Protected Dependency Index
| Element | Kind | Breaks if | Dependent injects / paths |
|---|---|---|---|
| host-a | host | unreachable | MSEL-01 |
| path-b | path | blocked | MSEL-02 |
"""
COMPLETE_MD = RANGE_MD + "\n".join(f"## {name}\n" for name in REQUIRED_SECTIONS)


def corpus():
    return Corpus(
        docs=(
            ExtractedDoc(path="a.txt", kind="text", text="x"),
            ExtractedDoc(path="b.pdf", kind="pdf", text="", error="corrupt"),
        )
    )


def clean_report():
    """A corpus and Range.md with nothing an operator needs to weigh."""
    docs = (ExtractedDoc(path="a.txt", kind="text", text="x"),)
    return build_report(Corpus(docs=docs), COMPLETE_MD, generated=False)


def test_report_states_what_was_ingested_and_what_is_missing():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert report.total_files == 2
    assert report.by_kind == (("text", 1),)
    assert report.unreadable == (("b.pdf", "corrupt"),)
    assert report.inject_count == 2
    assert report.index_rows == 2
    assert "Ingest Gaps" in report.range_md_gaps

    rendered = format_report(report)
    assert "b.pdf" in rendered
    assert "corrupt" in rendered
    assert "Ruling context" in rendered
    assert "every question" in rendered


def test_unreadable_truncated_or_oversized_input_counts_as_a_concern():
    clean = clean_report()
    assert clean.has_concerns() is False
    assert "re-sent with every question" not in format_report(clean)

    # Each case below has a complete Range.md, so the concern is the input itself.
    assert build_report(corpus(), COMPLETE_MD, generated=True).has_concerns() is True

    truncated = build_report(
        Corpus(docs=(
            ExtractedDoc(path="ok.txt", kind="text", text="fine"),
            ExtractedDoc(path="huge.log", kind="text", text="head…", truncated=True),
        )),
        COMPLETE_MD,
        generated=True,
    )
    assert truncated.truncated == ("huge.log",)
    assert truncated.has_concerns() is True
    assert "huge.log" in format_report(truncated)

    big = (ExtractedDoc(path="big.txt", kind="text", text="x" * (LARGE_CONTEXT_CHARS + 1)),)
    oversized = build_report(Corpus(docs=big), COMPLETE_MD, generated=False)
    assert oversized.context_chars > LARGE_CONTEXT_CHARS
    assert "re-sent with every question" in format_report(oversized)
    assert oversized.has_concerns() is True


def test_an_empty_dependency_index_warns_even_with_a_table_header():
    header_only = (
        "## Protected Dependency Index\n"
        "| Element | Kind | Breaks if | Dependent injects / paths |\n"
        "|:---:|:---:|:---:|:---:|\n"
    )
    for body in ("## Protected Dependency Index\n", header_only):
        report = build_report(corpus(), body, generated=True)
        assert report.index_rows == 0
        assert "Protected Dependency Index is empty" in format_report(report)


def test_confirm_prints_the_report_and_defaults_to_the_cautious_answer():
    concerning = build_report(corpus(), RANGE_MD, generated=True)

    out = io.StringIO()
    assert confirm(concerning, auto_yes=False, out=out, read_line=lambda: "") is False
    assert "b.pdf" in out.getvalue()
    assert "[y/N]" in out.getvalue()

    assert confirm(concerning, auto_yes=False, out=io.StringIO(), read_line=lambda: "y") is True
    assert confirm(concerning, auto_yes=True, out=io.StringIO(), read_line=lambda: "n") is True
    assert confirm(clean_report(), auto_yes=False, out=io.StringIO(), read_line=lambda: "") is True
