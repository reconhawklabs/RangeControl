import pytest

from rangecontrol.ingest.cache import ExtractionCache


def make(tmp_path, name="a.txt", content="original"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_miss_then_hit(tmp_path):
    cache = ExtractionCache(tmp_path / "cache")
    source = make(tmp_path)
    assert cache.get(source, "1") is None
    cache.put(source, "1", "extracted body")
    assert cache.get(source, "1") == "extracted body"


def test_changed_content_invalidates_entry(tmp_path):
    cache = ExtractionCache(tmp_path / "cache")
    source = make(tmp_path)
    cache.put(source, "1", "extracted body")
    source.write_text("different content", encoding="utf-8")
    assert cache.get(source, "1") is None


def test_bumped_version_invalidates_entry(tmp_path):
    cache = ExtractionCache(tmp_path / "cache")
    source = make(tmp_path)
    cache.put(source, "1", "extracted body")
    assert cache.get(source, "2") is None


def test_identical_content_at_different_paths_shares_entry(tmp_path):
    cache = ExtractionCache(tmp_path / "cache")
    first = make(tmp_path, "a.txt", "same bytes")
    second = make(tmp_path, "b.txt", "same bytes")
    cache.put(first, "1", "extracted body")
    assert cache.get(second, "1") == "extracted body"


def test_survives_a_new_cache_instance(tmp_path):
    cache_dir = tmp_path / "cache"
    source = make(tmp_path)
    ExtractionCache(cache_dir).put(source, "1", "extracted body")
    assert ExtractionCache(cache_dir).get(source, "1") == "extracted body"


def test_clear_removes_all_entries(tmp_path):
    cache = ExtractionCache(tmp_path / "cache")
    source = make(tmp_path)
    cache.put(source, "1", "extracted body")
    cache.clear()
    assert cache.get(source, "1") is None


def test_clear_raises_instead_of_silently_leaving_stale_entries(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(cache_dir)
    source = make(tmp_path)
    cache.put(source, "1", "extracted body")

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("rangecontrol.ingest.cache.shutil.rmtree", boom)
    with pytest.raises(OSError) as exc:
        cache.clear()
    assert str(cache_dir) in str(exc.value)


def test_unicode_round_trips(tmp_path):
    cache = ExtractionCache(tmp_path / "cache")
    source = make(tmp_path)
    cache.put(source, "1", "segment — Ω")
    assert cache.get(source, "1") == "segment — Ω"


def test_failed_write_leaves_the_previous_entry_intact(tmp_path, monkeypatch):
    cache = ExtractionCache(tmp_path / "cache")
    source = make(tmp_path)
    cache.put(source, "1", "good extraction")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("rangecontrol.ingest.cache.os.replace", boom)
    with pytest.raises(OSError):
        cache.put(source, "1", "replacement that never lands")

    assert cache.get(source, "1") == "good extraction"


def test_put_leaves_no_temporary_files(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(cache_dir)
    cache.put(make(tmp_path), "1", "extracted body")
    names = sorted(p.name for p in cache_dir.iterdir())
    assert len(names) == 1
    assert names[0].endswith(".txt")


def test_corrupt_entry_is_treated_as_a_miss(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = ExtractionCache(cache_dir)
    source = make(tmp_path)
    cache.put(source, "1", "extracted body")
    for entry in cache_dir.iterdir():
        entry.write_bytes(b"\xff\xfe not utf-8 \xff")
    assert cache.get(source, "1") is None
