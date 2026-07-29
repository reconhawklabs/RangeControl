import io

import pytest

from rangecontrol.purge import ExistingState, inspect_existing, prompt_purge


def cache_entry(cache_dir, name):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / name).write_text("extracted", encoding="utf-8")


# --- what a previous run left behind --------------------------------------


def test_a_fresh_directory_has_nothing_to_purge(tmp_path):
    state = inspect_existing(tmp_path, tmp_path / ".rangecontrol" / "cache")
    assert state.exists() is False


def test_finds_an_existing_range_md(tmp_path):
    (tmp_path / "Range.md").write_text("# Range", encoding="utf-8")
    state = inspect_existing(tmp_path, tmp_path / "cache")
    assert state.range_md is not None
    assert state.exists() is True


def test_finds_existing_cache_entries(tmp_path):
    cache = tmp_path / "cache"
    cache_entry(cache, "v1-aaa.txt")
    cache_entry(cache, "v1-bbb.txt")
    state = inspect_existing(tmp_path, cache)
    assert state.cache_entries == 2
    assert state.exists() is True


def test_an_empty_range_md_does_not_count(tmp_path):
    """prepare() already treats a blank Range.md as absent and regenerates.

    Offering to purge a file that is about to be ignored anyway would be
    describing a choice the operator does not actually have.
    """
    (tmp_path / "Range.md").write_text("   \n", encoding="utf-8")
    state = inspect_existing(tmp_path, tmp_path / "cache")
    assert state.range_md is None
    assert state.exists() is False


def test_partial_temp_files_are_not_counted_as_entries(tmp_path):
    cache = tmp_path / "cache"
    cache_entry(cache, "v1-aaa.txt")
    cache_entry(cache, "tmp123.tmp")
    state = inspect_existing(tmp_path, cache)
    assert state.cache_entries == 1


def test_an_empty_cache_directory_is_not_existing_state(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    state = inspect_existing(tmp_path, cache)
    assert state.exists() is False


# --- the prompt ------------------------------------------------------------


def ask(state, answer):
    out = io.StringIO()
    chose = prompt_purge(state, out=out, read_line=lambda: answer)
    return chose, out.getvalue()


def test_blank_enter_keeps_the_existing_files(tmp_path):
    """Purging costs API calls to rebuild, so the safe answer is the default."""
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=5)
    chose, text = ask(state, "")
    assert chose is False
    assert "[y/N]" in text


def test_yes_chooses_to_regenerate(tmp_path):
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=5)
    assert ask(state, "y")[0] is True
    assert ask(state, "YES")[0] is True


def test_no_keeps_the_existing_files(tmp_path):
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=5)
    assert ask(state, "n")[0] is False


def test_end_of_input_keeps_the_existing_files(tmp_path):
    """A closed stdin must not be read as consent to discard anything."""
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=5)
    out = io.StringIO()

    def closed():
        raise EOFError

    assert prompt_purge(state, out=out, read_line=closed) is False


def test_the_prompt_names_what_is_at_stake(tmp_path):
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=47)
    _, text = ask(state, "")
    assert "Range.md" in text
    assert "47" in text
    assert "backed up" in text.lower()


def test_the_prompt_omits_what_is_not_there(tmp_path):
    state = ExistingState(range_md=None, cache_entries=3)
    _, text = ask(state, "")
    assert "Range.md" not in text
    assert "3" in text


def test_the_prompt_warns_that_rebuilding_costs_api_calls(tmp_path):
    state = ExistingState(range_md=tmp_path / "Range.md", cache_entries=1)
    _, text = ask(state, "")
    assert "API" in text


def test_the_listed_path_is_absolute(tmp_path, monkeypatch):
    """RANGE_DIR is commonly ".", and "Range.md   Range.md" tells nobody where."""
    (tmp_path / "Range.md").write_text("# Range", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    state = inspect_existing(".", "cache")
    assert state.range_md is not None
    assert state.range_md.is_absolute()
