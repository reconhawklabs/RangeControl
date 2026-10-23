import pytest

from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.discovery import build_corpus
from rangecontrol.pregen import cache_put, cache_status, format_status
from tests.support.images import write_png
from tests.support.stub_provider import StubProvider


@pytest.fixture()
def resources(tmp_path):
    root = tmp_path / "resources"
    (root / "net").mkdir(parents=True)
    (root / "notes.txt").write_text("standing rules", encoding="utf-8")
    write_png(root / "net" / "diagram.png")
    return root


def test_pregenerated_entries_are_served_until_the_resource_changes(resources, tmp_path):
    cache_dir = tmp_path / "c"
    recorded = cache_put(resources, cache_dir, resources / "net" / "diagram.png", "two subnets, one firewall")
    assert recorded == "net/diagram.png"
    cache_put(resources, cache_dir, resources / "notes.txt", "stale text")
    (resources / "notes.txt").write_text("revised rules", encoding="utf-8")

    provider = StubProvider()
    corpus = build_corpus(resources, provider, ExtractionCache(cache_dir))

    bodies = {d.path: d.text for d in corpus.readable()}
    assert bodies["net/diagram.png"] == "two subnets, one firewall"
    assert provider.image_calls == []
    assert bodies["notes.txt"] == "revised rules"


def test_cache_put_rejects_outside_missing_or_empty_input(resources, tmp_path):
    outsider = tmp_path / "elsewhere.txt"
    outsider.write_text("x", encoding="utf-8")
    cases = [
        (outsider, "text"),
        (resources / "nope.txt", "text"),
        (resources / "notes.txt", "   "),
    ]
    for path, text in cases:
        with pytest.raises(ValueError):
            cache_put(resources, tmp_path / "c", path, text)


def test_status_names_exactly_the_uncached_files(resources, tmp_path):
    cache_dir = tmp_path / "c"
    empty = cache_status(resources, cache_dir)
    rendered = format_status(empty)
    assert "net/diagram.png" in rendered
    assert "2" in rendered

    cache_put(resources, cache_dir, resources / "notes.txt", "text")
    partial = cache_status(resources, cache_dir)
    assert partial.cached == ("notes.txt",)
    assert partial.missing == ("net/diagram.png",)

    cache_put(resources, cache_dir, resources / "net" / "diagram.png", "text")
    full = cache_status(resources, cache_dir)
    assert full.total == 2
    assert full.missing == ()
