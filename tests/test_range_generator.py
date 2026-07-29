import pytest

from rangecontrol.ingest.corpus import Corpus, ExtractedDoc
from rangecontrol.llm.base import LLMError
from rangecontrol.range_doc.generator import (
    GENERATOR_SYSTEM_PROMPT,
    generate_range_md,
    write_range_md,
)
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from tests.support.stub_provider import StubProvider

COMPLETE = "\n".join(f"## {name}\n\ncontent\n" for name in REQUIRED_SECTIONS)


def corpus():
    return Corpus(docs=(ExtractedDoc(path="notes.txt", kind="text", text="segment A"),))


def test_returns_generated_document():
    provider = StubProvider(completions=[COMPLETE])
    assert "Protected Dependency Index" in generate_range_md(corpus(), provider)


def test_corpus_text_is_sent_to_the_model():
    provider = StubProvider(completions=[COMPLETE])
    generate_range_md(corpus(), provider)
    assert "segment A" in provider.calls[0]["user"]


def test_template_is_sent_to_the_model():
    provider = StubProvider(completions=[COMPLETE])
    generate_range_md(corpus(), provider, template="## Custom Template")
    assert "## Custom Template" in provider.calls[0]["user"]


def test_generation_uses_a_generous_token_budget():
    provider = StubProvider(completions=[COMPLETE])
    generate_range_md(corpus(), provider)
    assert provider.calls[0]["max_tokens"] >= 16000


def test_prompt_forbids_inventing_facts():
    """Pins every load-bearing rule in GENERATOR_SYSTEM_PROMPT, not just the
    first one — this prompt is the disclosure control for what later reaches
    the adjudicating model, so a silent regression to any of its rules is as
    dangerous as a leak in the advisor prompts themselves."""
    lowered = GENERATOR_SYSTEM_PROMPT.lower()
    # Rule 1: sources-only; anything undetermined goes under Ingest Gaps
    # rather than being filled in with plausible-sounding content.
    assert "invent" in lowered or "do not guess" in lowered
    assert "ingest gaps" in lowered
    # Rule 3: identifiers survive verbatim — never paraphrased or normalised.
    assert "verbatim" in lowered
    # Rule 4: every MSEL/attack-path entry must declare its dependencies into
    # the Protected Dependency Index, the table that makes adjudication a
    # lookup instead of a re-derivation of the whole scenario.
    assert "protected dependency index" in lowered
    # Rule 5: contradictions between sources are recorded, never silently
    # resolved by picking one reading.
    assert "contradict" in lowered


def test_incomplete_output_raises_and_names_the_missing_sections():
    provider = StubProvider(completions=["## Exercise Overview\n\nonly this"])
    with pytest.raises(ValueError) as exc:
        generate_range_md(corpus(), provider)
    assert "Ingest Gaps" in str(exc.value)


def test_provider_failure_propagates():
    provider = StubProvider(error=LLMError("no route to host"))
    with pytest.raises(LLMError):
        generate_range_md(corpus(), provider)


def test_empty_corpus_raises_before_calling_the_model():
    provider = StubProvider(completions=[COMPLETE])
    with pytest.raises(ValueError):
        generate_range_md(Corpus(docs=()), provider)
    assert provider.calls == []


def test_write_creates_the_file_and_returns_its_path(tmp_path):
    path = write_range_md(tmp_path, "# Range\n")
    assert path == tmp_path / "Range.md"
    assert path.read_text(encoding="utf-8") == "# Range\n"


def test_write_backs_up_an_existing_file(tmp_path):
    (tmp_path / "Range.md").write_text("old", encoding="utf-8")
    write_range_md(tmp_path, "new")
    assert (tmp_path / "Range.md.bak").read_text(encoding="utf-8") == "old"
    assert (tmp_path / "Range.md").read_text(encoding="utf-8") == "new"


def test_repeated_writes_never_overwrite_an_earlier_backup(tmp_path):
    (tmp_path / "Range.md").write_text("operator hand-edited", encoding="utf-8")
    write_range_md(tmp_path, "generated one")
    write_range_md(tmp_path, "generated two")
    assert (tmp_path / "Range.md.bak").read_text(encoding="utf-8") == "operator hand-edited"
    assert (tmp_path / "Range.md.bak.1").read_text(encoding="utf-8") == "generated one"
    assert (tmp_path / "Range.md").read_text(encoding="utf-8") == "generated two"
