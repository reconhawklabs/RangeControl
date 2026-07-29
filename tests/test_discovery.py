import pytest

from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.discovery import build_corpus, discover
from rangecontrol.ingest.extractors.registry import EXTRACTOR_VERSION, extractor_for
from tests.support.stub_provider import StubProvider


@pytest.fixture()
def resources(tmp_path):
    root = tmp_path / "resources"
    (root / "network").mkdir(parents=True)
    (root / "notes.txt").write_text("standing rules", encoding="utf-8")
    (root / "network" / "topology.md").write_text("segment A to B", encoding="utf-8")
    (root / ".hidden.txt").write_text("ignore me", encoding="utf-8")
    return root


def test_discover_walks_recursively_and_sorts(resources):
    found = [p.relative_to(resources).as_posix() for p in discover(resources)]
    assert found == ["network/topology.md", "notes.txt"]


def test_discover_skips_hidden_files(resources):
    assert all(not p.name.startswith(".") for p in discover(resources))


def test_discover_skips_hidden_directories(resources):
    hidden = resources / ".git"
    hidden.mkdir()
    (hidden / "config").write_text("x", encoding="utf-8")
    assert all(".git" not in str(p) for p in discover(resources))


def test_discover_returns_empty_for_missing_directory(tmp_path):
    assert discover(tmp_path / "nope") == ()


def test_build_corpus_extracts_every_file(resources, tmp_path):
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    bodies = {d.path: d.text for d in corpus.readable()}
    assert bodies["notes.txt"] == "standing rules"
    assert bodies["network/topology.md"] == "segment A to B"


def test_paths_are_relative_and_posix_style(resources, tmp_path):
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    assert "network/topology.md" in {d.path for d in corpus.docs}


def test_unreadable_file_is_recorded_not_raised(resources, tmp_path):
    (resources / "broken.pdf").write_bytes(b"not a pdf")
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    unreadable = {d.path: d.error for d in corpus.unreadable()}
    assert "broken.pdf" in unreadable
    assert unreadable["broken.pdf"]
    # The other two files still extracted.
    assert len(corpus.readable()) == 2


def test_empty_text_file_is_recorded_as_unreadable(resources, tmp_path):
    (resources / "empty.txt").write_text("", encoding="utf-8")
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    assert "empty.txt" in {d.path for d in corpus.unreadable()}
    assert all(d.text.strip() for d in corpus.readable())


def test_whitespace_only_file_is_recorded_as_unreadable(resources, tmp_path):
    (resources / "blank.md").write_text("   \n\t\n", encoding="utf-8")
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    assert "blank.md" in {d.path for d in corpus.unreadable()}


def test_empty_cache_entry_is_recorded_as_unreadable(resources, tmp_path):
    cache = ExtractionCache(tmp_path / "c")
    target = resources / "notes.txt"
    kind, _ = extractor_for(target)
    cache.put(target, f"{EXTRACTOR_VERSION}-{kind}", "")
    corpus = build_corpus(resources, StubProvider(), cache)
    assert "notes.txt" in {d.path for d in corpus.unreadable()}


def test_no_readable_doc_ever_has_blank_text(resources, tmp_path):
    (resources / "empty.txt").write_text("", encoding="utf-8")
    (resources / "blank.md").write_text("  \n", encoding="utf-8")
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    rendered = corpus.to_prompt_text()
    assert "empty.txt" not in rendered
    assert "blank.md" not in rendered


def test_second_run_uses_cache_and_skips_the_provider(resources, tmp_path):
    (resources / "diagram.png").write_bytes(b"\x89PNG" + b"\x00" * 32)
    cache = ExtractionCache(tmp_path / "c")

    first = StubProvider(image_text="a diagram")
    build_corpus(resources, first, cache)
    assert len(first.image_calls) == 1

    second = StubProvider(image_text="a diagram")
    corpus = build_corpus(resources, second, cache)
    assert second.image_calls == []
    assert any(d.text == "a diagram" for d in corpus.readable())
