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


def test_generation_sends_corpus_and_template_with_a_generous_budget():
    provider = StubProvider(completions=[COMPLETE])
    result = generate_range_md(corpus(), provider, template="## Custom Template")
    assert "Protected Dependency Index" in result
    call = provider.calls[0]
    assert "segment A" in call["user"]
    assert "## Custom Template" in call["user"]
    assert call["max_tokens"] >= 16000


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


def test_generation_fails_loudly_rather_than_returning_a_partial_document():
    incomplete = StubProvider(completions=["## Exercise Overview\n\nonly this"])
    with pytest.raises(ValueError) as exc:
        generate_range_md(corpus(), incomplete)
    assert "Ingest Gaps" in str(exc.value)

    with pytest.raises(LLMError):
        generate_range_md(corpus(), StubProvider(error=LLMError("no route to host")))

    unused = StubProvider(completions=[COMPLETE])
    with pytest.raises(ValueError):
        generate_range_md(Corpus(docs=()), unused)
    assert unused.calls == []


def test_writes_never_lose_an_earlier_range_md(tmp_path):
    (tmp_path / "Range.md").write_text("operator hand-edited", encoding="utf-8")
    path = write_range_md(tmp_path, "generated one")
    write_range_md(tmp_path, "generated two")
    assert path == tmp_path / "Range.md"
    assert (tmp_path / "Range.md.bak").read_text(encoding="utf-8") == "operator hand-edited"
    assert (tmp_path / "Range.md.bak.1").read_text(encoding="utf-8") == "generated one"
    assert path.read_text(encoding="utf-8") == "generated two"
