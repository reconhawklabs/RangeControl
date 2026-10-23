import pytest

from rangecontrol.ingest.corpus import Corpus, ExtractedDoc


def doc(path="a.txt", kind="text", text="body", error=None):
    return ExtractedDoc(path=path, kind=kind, text=text, error=error)


def test_errored_docs_are_partitioned_out_of_readable_and_kind_counts():
    corpus = Corpus(
        docs=(doc("a.txt"), doc("b.txt"), doc("c.pdf", kind="pdf"), doc("d.pdf", kind="pdf", error="x"))
    )
    assert [d.path for d in corpus.readable()] == ["a.txt", "b.txt", "c.pdf"]
    assert [d.path for d in corpus.unreadable()] == ["d.pdf"]
    assert corpus.counts_by_kind() == (("pdf", 1), ("text", 2))


def test_prompt_text_is_byte_stable_and_omits_unreadable_docs():
    """The rendered corpus is a cached prompt prefix: any drift in framing or
    order between runs is a cache miss on every ruling."""
    corpus = Corpus(
        docs=(
            doc("net/topology.txt", text="segment A\n"),
            doc("bad.pdf", text="", error="corrupt"),
            doc("notes.txt", text="rules"),
        )
    )
    expected = (
        "===== RESOURCE: net/topology.txt =====\nsegment A"
        "\n\n"
        "===== RESOURCE: notes.txt =====\nrules"
    )
    assert corpus.to_prompt_text() == expected
    assert Corpus(docs=()).to_prompt_text() == ""


def test_corpus_is_immutable_and_with_doc_returns_a_copy():
    original = Corpus(docs=(doc("a.txt"),))
    extended = original.with_doc(doc("b.txt"))
    assert len(original.docs) == 1
    assert len(extended.docs) == 2
    with pytest.raises(Exception):
        doc().text = "mutated"  # type: ignore[misc]
    with pytest.raises(Exception):
        original.docs = ()  # type: ignore[misc]
