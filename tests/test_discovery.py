import pytest

from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.discovery import MAX_DOC_CHARS, _record, build_corpus, discover
from rangecontrol.ingest.extractors.embedded import EMBEDDED_IMAGES_HEADER
from rangecontrol.ingest.extractors.registry import cache_version, extractor_for
from tests.support.images import write_png
from tests.support.stub_provider import StubProvider


@pytest.fixture()
def resources(tmp_path):
    root = tmp_path / "resources"
    (root / "network").mkdir(parents=True)
    (root / "notes.txt").write_text("standing rules", encoding="utf-8")
    (root / "network" / "topology.md").write_text("segment A to B", encoding="utf-8")
    (root / ".hidden.txt").write_text("ignore me", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")
    return root


def test_discover_walks_recursively_sorted_and_skips_hidden_paths(resources, tmp_path):
    found = [p.relative_to(resources).as_posix() for p in discover(resources)]
    assert found == ["network/topology.md", "notes.txt"]
    assert discover(tmp_path / "nope") == ()


def test_unreadable_and_blank_files_are_recorded_never_raised_or_rendered(resources, tmp_path):
    """No readable doc may have blank text: a blank body would render into the
    model context as a header for a document the adjudicator never received."""
    (resources / "broken.pdf").write_bytes(b"not a pdf")
    (resources / "empty.txt").write_text("", encoding="utf-8")
    (resources / "blank.md").write_text("   \n\t\n", encoding="utf-8")
    replayed = resources / "replayed.txt"
    replayed.write_text("real content", encoding="utf-8")
    cache = ExtractionCache(tmp_path / "c")
    cache.put(replayed, cache_version(extractor_for(replayed)[0]), "")

    corpus = build_corpus(resources, StubProvider(), cache)

    assert {d.path: d.text for d in corpus.readable()} == {
        "network/topology.md": "segment A to B",
        "notes.txt": "standing rules",
    }
    unreadable = {d.path: d.error for d in corpus.unreadable()}
    assert set(unreadable) == {"broken.pdf", "empty.txt", "blank.md", "replayed.txt"}
    assert all(unreadable.values())
    rendered = corpus.to_prompt_text()
    for name in unreadable:
        assert name not in rendered


def test_second_run_uses_cache_skips_the_provider_and_renders_identical_bytes(resources, tmp_path):
    """The rendered corpus is the cached prompt prefix: a cache replay that
    differed by one byte from the fresh extraction would miss it every run."""
    write_png(resources / "diagram.png")
    cache = ExtractionCache(tmp_path / "c")

    first = StubProvider(image_text="a diagram")
    fresh = build_corpus(resources, first, cache)
    assert len(first.image_calls) == 1

    second = StubProvider(image_text="a diagram")
    replayed = build_corpus(resources, second, cache)
    assert second.image_calls == []
    assert any(d.text == "a diagram" for d in replayed.readable())
    assert replayed.to_prompt_text() == fresh.to_prompt_text()


def test_an_enormous_file_is_truncated_flagged_and_keeps_image_descriptions(resources, tmp_path):
    """A 300 MB packet log would otherwise go into every ruling's prompt and
    fail the API call with an unhelpful 400. The head is kept, the cut is
    marked in the text, and the report can name the file. Embedded image
    descriptions are paid for, so the cut lands in the text ahead of them."""
    (resources / "huge.log").write_text("x" * (MAX_DOC_CHARS + 500), encoding="utf-8")
    corpus = build_corpus(resources, StubProvider(), ExtractionCache(tmp_path / "c"))
    doc = next(d for d in corpus.docs if d.path == "huge.log")
    assert doc.error is None
    assert doc.truncated
    assert len(doc.text) < MAX_DOC_CHARS + 200
    assert "truncated" in doc.text[-200:].lower()
    assert corpus.truncated_docs() == (doc,)

    images = f"{EMBEDDED_IMAGES_HEADER}\n[page 1 image 1]\nFW-1 joins HQ-LAN to DMZ."
    brief = _record("brief.pdf", "pdf", f"{'t' * (MAX_DOC_CHARS + 1000)}\n\n{images}")
    assert brief.truncated
    assert brief.text.endswith("FW-1 joins HQ-LAN to DMZ.")
    assert "TRUNCATED" in brief.text
    assert len(brief.text) <= MAX_DOC_CHARS + 400
