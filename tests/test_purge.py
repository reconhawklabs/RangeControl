import io

from rangecontrol.purge import ExistingState, inspect_existing, prompt_purge


def cache_entry(cache_dir, name):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / name).write_text("extracted", encoding="utf-8")


def ask(state, answer):
    out = io.StringIO()
    chose = prompt_purge(state, out=out, read_line=lambda: answer)
    return chose, out.getvalue()


def test_nothing_ignorable_is_offered_for_purge(tmp_path):
    """A blank Range.md is already treated as absent and regenerated, and an
    empty cache holds nothing; offering to purge either describes a choice
    the operator does not actually have."""
    cache = tmp_path / "cache"
    assert inspect_existing(tmp_path, cache).exists() is False

    cache.mkdir()
    (tmp_path / "Range.md").write_text("   \n", encoding="utf-8")
    state = inspect_existing(tmp_path, cache)
    assert state.range_md is None
    assert state.exists() is False


def test_inspect_counts_only_committed_state_and_touches_nothing(tmp_path, monkeypatch):
    """A leftover .tmp is a crashed write, not a cached resource. RANGE_DIR is
    commonly ".", and "Range.md   Range.md" tells nobody where the file is."""
    (tmp_path / "Range.md").write_text("# Range", encoding="utf-8")
    cache = tmp_path / "cache"
    cache_entry(cache, "v1-aaa.txt")
    cache_entry(cache, "v1-bbb.txt")
    cache_entry(cache, "tmp123.tmp")
    before = sorted(p.name for p in cache.iterdir())

    monkeypatch.chdir(tmp_path)
    state = inspect_existing(".", "cache")

    assert state.exists() is True
    assert state.cache_entries == 2
    assert state.range_md is not None and state.range_md.is_absolute()
    assert (tmp_path / "Range.md").read_text(encoding="utf-8") == "# Range"
    assert sorted(p.name for p in cache.iterdir()) == before


def test_only_an_explicit_yes_discards_existing_state(tmp_path):
    """Purging costs API calls to rebuild, so keeping is the default, and a
    closed stdin must not be read as consent to discard anything."""
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=5)
    for answer in ("", "n"):
        assert ask(state, answer)[0] is False
    for answer in ("y", "YES"):
        assert ask(state, answer)[0] is True

    def closed():
        raise EOFError

    assert prompt_purge(state, out=io.StringIO(), read_line=closed) is False


def test_the_prompt_names_exactly_what_is_at_stake(tmp_path):
    _, text = ask(ExistingState(range_md=tmp_path / "Range.md", cache_entries=47), "")
    assert "Range.md" in text
    assert "47" in text
    assert "backed up" in text.lower()
    assert "API" in text
    assert "[y/N]" in text

    _, text = ask(ExistingState(range_md=None, cache_entries=3), "")
    assert "Range.md" not in text
    assert "3" in text
