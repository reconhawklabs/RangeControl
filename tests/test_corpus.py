import pytest

from rangecontrol.ingest.corpus import Corpus, ExtractedDoc


def doc(path="a.txt", kind="text", text="body", error=None):
    return ExtractedDoc(path=path, kind=kind, text=text, error=error)


def test_readable_excludes_errored_docs():
    corpus = Corpus(docs=(doc("a.txt"), doc("b.pdf", error="unreadable")))
    assert [d.path for d in corpus.readable()] == ["a.txt"]
    assert [d.path for d in corpus.unreadable()] == ["b.pdf"]


def test_counts_by_kind_is_sorted_and_ignores_errors():
    corpus = Corpus(
        docs=(doc("a.txt"), doc("b.txt"), doc("c.pdf", kind="pdf"), doc("d.pdf", kind="pdf", error="x"))
    )
    assert corpus.counts_by_kind() == (("pdf", 1), ("text", 2))


def test_prompt_text_includes_path_headers_and_bodies():
    corpus = Corpus(docs=(doc("net/topology.txt", text="segment A"),))
    rendered = corpus.to_prompt_text()
    assert "net/topology.txt" in rendered
    assert "segment A" in rendered


def test_prompt_text_omits_unreadable_docs():
    corpus = Corpus(docs=(doc("bad.pdf", text="", error="corrupt"),))
    assert "bad.pdf" not in corpus.to_prompt_text()


def test_empty_corpus_renders_empty_string():
    assert Corpus(docs=()).to_prompt_text() == ""


def test_with_doc_returns_new_corpus_and_leaves_original_untouched():
    original = Corpus(docs=(doc("a.txt"),))
    extended = original.with_doc(doc("b.txt"))
    assert len(original.docs) == 1
    assert len(extended.docs) == 2
    assert extended is not original


def test_objects_are_frozen():
    with pytest.raises(Exception):
        doc().text = "mutated"  # type: ignore[misc]
    with pytest.raises(Exception):
        Corpus(docs=()).docs = ()  # type: ignore[misc]
