import os
from unittest.mock import patch

from rangecontrol.gui.env_file import read_env, update_env


def test_reads_simple_pairs(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=gemini\nLLM_API_KEY=abc123\n", encoding="utf-8")
    assert read_env(env) == {"LLM_PROVIDER": "gemini", "LLM_API_KEY": "abc123"}


def test_missing_file_reads_as_empty(tmp_path):
    assert read_env(tmp_path / "nope.env") == {}


def test_ignores_comments_and_blank_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# a comment\n\nLLM_PROVIDER=gemini\n", encoding="utf-8")
    assert read_env(env) == {"LLM_PROVIDER": "gemini"}


def test_strips_surrounding_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('LLM_API_KEY="abc 123"\nX=\'y\'\n', encoding="utf-8")
    assert read_env(env) == {"LLM_API_KEY": "abc 123", "X": "y"}


def test_update_preserves_comments_and_key_order(tmp_path):
    """The user may have annotated their own .env. Rewriting must not erase it."""
    env = tmp_path / ".env"
    env.write_text(
        "# RangeControl config\nLLM_PROVIDER=anthropic\n\n"
        "# my note\nLLM_API_KEY=old\n",
        encoding="utf-8",
    )
    update_env(env, {"LLM_API_KEY": "new"})
    text = env.read_text(encoding="utf-8")
    assert "# RangeControl config" in text
    assert "# my note" in text
    assert text.index("LLM_PROVIDER") < text.index("LLM_API_KEY")
    assert read_env(env)["LLM_API_KEY"] == "new"


def test_update_appends_unknown_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=gemini\n", encoding="utf-8")
    update_env(env, {"WHITE_CELL_CHANNEL_ID": "42"})
    assert read_env(env)["WHITE_CELL_CHANNEL_ID"] == "42"


def test_update_creates_a_missing_file(tmp_path):
    env = tmp_path / ".env"
    update_env(env, {"LLM_PROVIDER": "gemini"})
    assert read_env(env) == {"LLM_PROVIDER": "gemini"}


def test_keys_the_user_added_by_hand_survive(tmp_path):
    """RANGE_DIR and LLM_GATE_MODEL are not in the form but must not be lost."""
    env = tmp_path / ".env"
    env.write_text("LLM_GATE_MODEL=cheap-model\nLLM_PROVIDER=gemini\n", encoding="utf-8")
    update_env(env, {"LLM_PROVIDER": "anthropic"})
    assert read_env(env)["LLM_GATE_MODEL"] == "cheap-model"


def test_values_needing_quotes_round_trip(tmp_path):
    env = tmp_path / ".env"
    update_env(env, {"K": "has spaces and # hash"})
    assert read_env(env)["K"] == "has spaces and # hash"


def test_temp_file_cleaned_on_replace_failure(tmp_path):
    """A crash mid-write must not leave a truncated .env holding half a token."""
    env = tmp_path / ".env"
    # Write initial content successfully
    update_env(env, {"A": "1"})
    initial_content = env.read_text(encoding="utf-8")

    # Monkeypatch os.replace to raise an exception
    def failing_replace(src, dst):
        raise RuntimeError("Simulated crash during atomic rename")

    with patch("os.replace", side_effect=failing_replace):
        try:
            update_env(env, {"A": "2", "B": "new"})
        except RuntimeError:
            pass  # Expected

    # File must be unchanged and no temp file should remain
    assert env.read_text(encoding="utf-8") == initial_content
    assert read_env(env) == {"A": "1"}
    assert not list(tmp_path.glob("*.tmp"))


def test_double_quote_in_value_round_trips(tmp_path):
    """Values with double quotes must survive round-trip."""
    env = tmp_path / ".env"
    update_env(env, {"K": 'say "hi" ok'})
    assert read_env(env)["K"] == 'say "hi" ok'


def test_backslash_in_value_round_trips(tmp_path):
    """Values with backslashes must survive round-trip."""
    env = tmp_path / ".env"
    update_env(env, {"K": r'a\backslash and space'})
    assert read_env(env)["K"] == r'a\backslash and space'


def test_single_quote_character_round_trips(tmp_path):
    """A value that is a single double-quote character must survive."""
    env = tmp_path / ".env"
    update_env(env, {"K": '"'})
    assert read_env(env)["K"] == '"'


def test_newline_in_value_round_trips(tmp_path):
    """Values with embedded newlines must survive round-trip."""
    env = tmp_path / ".env"
    update_env(env, {"K": "line1\nline2"})
    assert read_env(env)["K"] == "line1\nline2"


def test_newline_does_not_lose_following_key(tmp_path):
    """A newline in one value must not cause the next key to be lost."""
    env = tmp_path / ".env"
    update_env(env, {"LLM_API_KEY": "line1\nline2", "OTHER": "x"})
    result = read_env(env)
    assert result["LLM_API_KEY"] == "line1\nline2"
    assert result["OTHER"] == "x"


def test_backslash_before_n_round_trips(tmp_path):
    """Backslash before a literal 'n' must not become a newline."""
    env = tmp_path / ".env"
    update_env(env, {"RANGE_DIR": r"C:\nathan\My Data"})
    assert read_env(env)["RANGE_DIR"] == r"C:\nathan\My Data"


def test_backslash_before_r_round_trips(tmp_path):
    """Backslash before a literal 'r' must not become a carriage return."""
    env = tmp_path / ".env"
    update_env(env, {"PATH": r"C:\raw\path"})
    assert read_env(env)["PATH"] == r"C:\raw\path"


def test_trailing_backslash_round_trips(tmp_path):
    """A value ending with a backslash must survive round-trip."""
    env = tmp_path / ".env"
    update_env(env, {"K": "trailing backslash\\"})
    assert read_env(env)["K"] == "trailing backslash\\"


def test_double_backslash_round_trips(tmp_path):
    """Two literal backslashes must survive round-trip."""
    env = tmp_path / ".env"
    update_env(env, {"K": r"\\"})
    assert read_env(env)["K"] == r"\\"


def test_mixed_escapes_round_trip(tmp_path):
    """A value mixing quotes, backslashes, and real newlines."""
    env = tmp_path / ".env"
    value = 'mix "quotes" and \\n and a real\nnewline'
    update_env(env, {"K": value})
    assert read_env(env)["K"] == value


def test_backslash_n_sentinel_pair_round_trips(tmp_path):
    """Backslash-n pair followed by sentinel key."""
    env = tmp_path / ".env"
    update_env(env, {"FIRST": r"has\n", "SECOND": "sentinel"})
    result = read_env(env)
    assert result["FIRST"] == r"has\n"
    assert result["SECOND"] == "sentinel"


def test_property_all_escape_combinations_round_trip(tmp_path):
    """Property test: all combinations from the escape alphabet round-trip.

    This tests the entire class of potential ambiguous sequences by combining
    the 'n' and 'r' characters (which appear in our escape sequences) with
    other characters that might appear after a literal backslash.
    """
    env = tmp_path / ".env"
    # Alphabet includes: empty, literal letters, special chars, escape triggers
    alphabet = ["", "a", "n", "r", '"', "\\", " ", "#", "\n", "\r", "'", "\t", "="]

    test_count = 0
    for prefix in alphabet:
        for middle in alphabet:
            for suffix in alphabet:
                value = prefix + middle + suffix
                key = f"KEY_{test_count}"

                update_env(env, {key: value})
                result = read_env(env)

                assert result[key] == value, (
                    f"Value {value!r} for {key} round-tripped as {result[key]!r}"
                )
                test_count += 1

    # Verify we tested a reasonable number of combinations
    assert test_count >= len(alphabet) ** 3 - 1  # All combos minus one
