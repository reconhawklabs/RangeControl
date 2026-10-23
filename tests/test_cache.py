import pytest

from rangecontrol.ingest.cache import ExtractionCache


def make(tmp_path, name="a.txt", content="original"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_entries_are_content_addressed_and_never_served_stale(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(cache_dir)
    source = make(tmp_path)
    assert cache.get(source, "1") is None
    cache.put(source, "1", "segment — Ω")
    assert cache.get(source, "1") == "segment — Ω"
    # Persistent across instances, and keyed by bytes rather than path.
    assert ExtractionCache(cache_dir).get(source, "1") == "segment — Ω"
    assert cache.get(make(tmp_path, "copy.txt", "original"), "1") == "segment — Ω"

    # An extractor bump or an edited resource is a miss, never the old text.
    assert cache.get(source, "2") is None
    source.write_text("different content", encoding="utf-8")
    assert cache.get(source, "1") is None

    # A corrupt entry is a miss, not an exception.
    cache.put(source, "1", "fresh body")
    for entry in cache_dir.iterdir():
        entry.write_bytes(b"\xff\xfe not utf-8 \xff")
    assert cache.get(source, "1") is None


def test_writes_are_atomic(tmp_path, monkeypatch):
    """A truncated entry would be served as a valid hit and reach the
    adjudicating model looking complete."""
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(cache_dir)
    source = make(tmp_path)
    cache.put(source, "1", "good extraction")
    assert [p.suffix for p in cache_dir.iterdir()] == [".txt"]

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("rangecontrol.ingest.cache.os.replace", boom)
    with pytest.raises(OSError):
        cache.put(source, "1", "replacement that never lands")
    assert cache.get(source, "1") == "good extraction"
    assert [p.suffix for p in cache_dir.iterdir()] == [".txt"]


def test_clear_removes_only_cache_entries_and_raises_on_partial_failure(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(cache_dir)
    source = make(tmp_path)
    neighbour = make(tmp_path, "Range.md", "# Range")
    cache.put(source, "1", "extracted body")
    cache.clear()
    assert cache.get(source, "1") is None
    assert source.read_text(encoding="utf-8") == "original"
    assert neighbour.read_text(encoding="utf-8") == "# Range"

    cache.put(source, "1", "extracted body")

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("rangecontrol.ingest.cache.shutil.rmtree", boom)
    with pytest.raises(OSError) as exc:
        cache.clear()
    assert str(cache_dir) in str(exc.value)
