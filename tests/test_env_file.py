import os
import stat
from unittest.mock import patch

import pytest

from rangecontrol.gui.env_file import read_env, update_env


def test_read_env_parses_pairs_and_skips_comments_and_quotes(tmp_path):
    assert read_env(tmp_path / "nope.env") == {}

    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n\nLLM_PROVIDER=gemini\nLLM_API_KEY=\"abc 123\"\nX='y'\n",
        encoding="utf-8",
    )
    assert read_env(env) == {"LLM_PROVIDER": "gemini", "LLM_API_KEY": "abc 123", "X": "y"}


def test_update_preserves_everything_the_user_wrote(tmp_path):
    """The user may have annotated their own .env, and keys the form does not
    show (LLM_GATE_MODEL, RANGE_DIR) must not vanish on the first edit."""
    env = tmp_path / ".env"
    env.write_text(
        "# RangeControl config\nLLM_GATE_MODEL=cheap-model\nLLM_PROVIDER=anthropic\n\n"
        "# my note\nLLM_API_KEY=old\n",
        encoding="utf-8",
    )
    update_env(env, {"LLM_API_KEY": "new"})

    text = env.read_text(encoding="utf-8")
    assert "# RangeControl config" in text
    assert "# my note" in text
    assert text.index("LLM_PROVIDER") < text.index("LLM_API_KEY")
    assert read_env(env) == {
        "LLM_GATE_MODEL": "cheap-model", "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "new",
    }


def test_update_creates_a_private_file_and_appends_new_keys(tmp_path):
    env = tmp_path / ".env"
    update_env(env, {"LLM_PROVIDER": "gemini"})
    update_env(env, {"WHITE_CELL_CHANNEL_ID": "42"})
    assert read_env(env) == {"LLM_PROVIDER": "gemini", "WHITE_CELL_CHANNEL_ID": "42"}
    if os.name == "posix":
        # It holds the bot token and API key.
        assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_a_failed_replace_leaves_the_old_env_and_no_temp_file(tmp_path):
    """A crash mid-write must not leave a truncated .env holding half a token."""
    env = tmp_path / ".env"
    update_env(env, {"A": "1"})
    initial_content = env.read_text(encoding="utf-8")

    with patch("os.replace", side_effect=RuntimeError("crash during rename")):
        with pytest.raises(RuntimeError):
            update_env(env, {"A": "2", "B": "new"})

    assert env.read_text(encoding="utf-8") == initial_content
    assert read_env(env) == {"A": "1"}
    assert not list(tmp_path.glob("*.tmp"))


def test_every_escape_combination_round_trips(tmp_path):
    """Every 3-character string over the escape alphabet survives a write and
    a read, which covers quotes, backslash-n vs a real newline, trailing and
    doubled backslashes, and everything that would need quoting."""
    env = tmp_path / ".env"
    alphabet = ["", "a", "n", "r", '"', "\\", " ", "#", "\n", "\r", "'", "\t", "="]

    count = 0
    for prefix in alphabet:
        for middle in alphabet:
            for suffix in alphabet:
                key = f"KEY_{count}"
                value = prefix + middle + suffix
                update_env(env, {key: value})
                result = read_env(env)[key]
                assert result == value, f"{value!r} round-tripped as {result!r}"
                count += 1


def test_realistic_secrets_round_trip_without_swallowing_the_next_key(tmp_path):
    """A newline or trailing backslash in one value must not eat the line after it."""
    env = tmp_path / ".env"
    values = {
        "LLM_API_KEY": "line1\nline2",
        "AFTER_NEWLINE": "sentinel",
        "DISCORD_BOT_TOKEN": 'say "hi" ok',
        "RANGE_DIR": r"C:\nathan\My Data",
        "RAW": r"C:\raw\path",
        "TRAILING": "trailing backslash\\",
        "BACKSLASH_N": r"has\n",
        "AFTER_BACKSLASH_N": "sentinel",
        "HASH": "has spaces and # hash",
    }
    update_env(env, values)
    assert read_env(env) == values
