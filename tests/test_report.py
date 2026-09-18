import io

from rangecontrol.ingest.corpus import Corpus, ExtractedDoc
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from rangecontrol.range_doc.report import build_report, confirm, format_report

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
    body = RANGE_MD + "\n".join(f"## {name}\n" for name in REQUIRED_SECTIONS)
    return build_report(Corpus(docs=docs), body, generated=False)


def test_counts_files_and_kinds():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert report.total_files == 2
    assert report.by_kind == (("text", 1),)


def test_lists_unreadable_files_with_reasons():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert report.unreadable == (("b.pdf", "corrupt"),)


def test_counts_injects_and_index_rows():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert report.inject_count == 2
    assert report.index_rows == 2


def test_reports_missing_sections_as_gaps():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert "Ingest Gaps" in report.range_md_gaps


def test_formatted_output_surfaces_key_facts():
    rendered = format_report(build_report(corpus(), RANGE_MD, generated=True))
    assert "b.pdf" in rendered
    assert "corrupt" in rendered
    assert "2" in rendered


def test_formatted_output_warns_when_index_is_empty():
    report = build_report(corpus(), "## Protected Dependency Index\n", generated=True)
    assert "WARNING" in format_report(report).upper()


def test_auto_yes_skips_the_prompt():
    out = io.StringIO()
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert confirm(report, auto_yes=True, out=out, read_line=lambda: "n") is True


def test_clean_ingest_has_no_concerns():
    assert clean_report().has_concerns() is False


def test_report_with_unreadable_files_has_concerns():
    assert build_report(corpus(), RANGE_MD, generated=True).has_concerns() is True


def test_blank_input_accepts_a_clean_ingest():
    assert confirm(clean_report(), auto_yes=False, out=io.StringIO(), read_line=lambda: "") is True


def test_blank_input_declines_when_there_are_concerns():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert confirm(report, auto_yes=False, out=io.StringIO(), read_line=lambda: "") is False


def test_explicit_yes_accepts_despite_concerns():
    report = build_report(corpus(), RANGE_MD, generated=True)
    assert confirm(report, auto_yes=False, out=io.StringIO(), read_line=lambda: "y") is True


def test_prompt_shows_the_cautious_default_when_there_are_concerns():
    out = io.StringIO()
    confirm(build_report(corpus(), RANGE_MD, generated=True), False, out, lambda: "n")
    assert "[y/N]" in out.getvalue()


def test_empty_index_written_with_alignment_row_still_warns():
    body = (
        "## Protected Dependency Index\n"
        "| Element | Kind | Breaks if | Dependent injects / paths |\n"
        "|:---:|:---:|:---:|:---:|\n"
    )
    report = build_report(corpus(), body, generated=True)
    assert report.index_rows == 0
    assert "Protected Dependency Index is empty" in format_report(report)


def test_report_is_printed_before_prompting():
    out = io.StringIO()
    confirm(build_report(corpus(), RANGE_MD, generated=True), False, out, lambda: "")
    assert "b.pdf" in out.getvalue()


def test_report_shows_the_per_question_context_size():
    docs = (ExtractedDoc(path="a.txt", kind="text", text="x" * 4000),)
    report = build_report(Corpus(docs=docs), RANGE_MD, generated=False)
    assert report.context_chars >= 4000
    assert "Ruling context" in format_report(report)
    assert "every question" in format_report(report)


def test_large_context_warns_and_counts_as_a_concern():
    from rangecontrol.range_doc.report import LARGE_CONTEXT_CHARS

    docs = (ExtractedDoc(path="big.txt", kind="text", text="x" * (LARGE_CONTEXT_CHARS + 1)),)
    report = build_report(Corpus(docs=docs), RANGE_MD, generated=False)
    rendered = format_report(report)
    assert "re-sent with every question" in rendered
    assert report.has_concerns() is True


def test_small_context_does_not_warn():
    assert "re-sent with every question" not in format_report(clean_report())


def test_truncated_files_are_listed_and_count_as_a_concern():
    from rangecontrol.ingest.corpus import Corpus, ExtractedDoc

    docs = (
        ExtractedDoc(path="ok.txt", kind="text", text="fine"),
        ExtractedDoc(path="huge.log", kind="text", text="head…", truncated=True),
    )
    report = build_report(Corpus(docs=docs), RANGE_MD, generated=True)
    assert report.truncated == ("huge.log",)
    assert report.has_concerns() is True
    assert "huge.log" in format_report(report)
