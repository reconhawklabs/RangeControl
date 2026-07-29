import pytest

from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.discovery import build_corpus
from rangecontrol.pregen import cache_put, cache_status, format_status
from tests.support.stub_provider import StubProvider


@pytest.fixture()
def resources(tmp_path):
    root = tmp_path / "resources"
    (root / "net").mkdir(parents=True)
    (root / "notes.txt").write_text("standing rules", encoding="utf-8")
    (root / "net" / "diagram.png").write_bytes(b"\x89PNG" + b"\x00" * 32)
    return root


def test_cache_put_returns_relative_posix_path(resources, tmp_path):
    recorded = cache_put(resources, tmp_path / "c", resources / "net" / "diagram.png", "two subnets")
    assert recorded == "net/diagram.png"


def test_bot_reads_externally_written_entry_without_calling_provider(resources, tmp_path):
    cache_dir = tmp_path / "c"
    cache_put(resources, cache_dir, resources / "net" / "diagram.png", "two subnets, one firewall")
    cache_put(resources, cache_dir, resources / "notes.txt", "standing rules")

    provider = StubProvider(error=AssertionError("provider must not be called"))
    corpus = build_corpus(resources, provider, ExtractionCache(cache_dir))

    bodies = {d.path: d.text for d in corpus.readable()}
    assert bodies["net/diagram.png"] == "two subnets, one firewall"
    assert provider.image_calls == []


def test_edited_resource_invalidates_the_external_entry(resources, tmp_path):
    cache_dir = tmp_path / "c"
    cache_put(resources, cache_dir, resources / "notes.txt", "stale text")
    (resources / "notes.txt").write_text("revised rules", encoding="utf-8")

    corpus = build_corpus(resources, StubProvider(), ExtractionCache(cache_dir))
    bodies = {d.path: d.text for d in corpus.readable()}
    assert bodies["notes.txt"] == "revised rules"


def test_resource_outside_the_directory_is_rejected(resources, tmp_path):
    outsider = tmp_path / "elsewhere.txt"
    outsider.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        cache_put(resources, tmp_path / "c", outsider, "text")


def test_missing_resource_is_rejected(resources, tmp_path):
    with pytest.raises(ValueError):
        cache_put(resources, tmp_path / "c", resources / "nope.txt", "text")


def test_empty_text_is_rejected(resources, tmp_path):
    with pytest.raises(ValueError):
        cache_put(resources, tmp_path / "c", resources / "notes.txt", "   ")


def test_status_reports_full_coverage(resources, tmp_path):
    cache_dir = tmp_path / "c"
    for name in ("notes.txt", "net/diagram.png"):
        cache_put(resources, cache_dir, resources / name, "text")
    status = cache_status(resources, cache_dir)
    assert status.total == 2
    assert status.missing == ()


def test_status_lists_uncached_files(resources, tmp_path):
    cache_dir = tmp_path / "c"
    cache_put(resources, cache_dir, resources / "notes.txt", "text")
    status = cache_status(resources, cache_dir)
    assert status.cached == ("notes.txt",)
    assert status.missing == ("net/diagram.png",)


def test_format_status_names_the_missing_files(resources, tmp_path):
    status = cache_status(resources, tmp_path / "c")
    rendered = format_status(status)
    assert "net/diagram.png" in rendered
    assert "2" in rendered
