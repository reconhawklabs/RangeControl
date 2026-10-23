import pytest

from rangecontrol.config import ConfigError, load_config
from rangecontrol.gui.settings import (
    FIELDS,
    is_configured,
    missing_required,
    models_for,
    to_config_mapping,
)

COMPLETE = {
    "LLM_PROVIDER": "gemini",
    "LLM_MODEL": "gemini-2.5-pro",
    "LLM_API_KEY": "key",
    "DISCORD_BOT_TOKEN": "tok",
    "WHITE_CELL_CHANNEL_ID": "42",
}


def test_exactly_the_two_credentials_are_masked_secrets():
    secret = {f.key for f in FIELDS if f.secret}
    assert secret == {"LLM_API_KEY", "DISCORD_BOT_TOKEN"}


def test_missing_required_blocks_start_until_every_gap_is_filled():
    """is_configured gates Start Bot, so it must agree with load_config."""
    assert missing_required({}) == (
        "AI provider",
        "API key",
        "Discord bot token",
        "White cell channel ID",
    )
    assert is_configured({}) is False

    assert missing_required(COMPLETE) == ()
    assert is_configured(COMPLETE) is True

    assert "API key" in missing_required({**COMPLETE, "LLM_API_KEY": "   "})

    # load_config requires a provider, so a blank one is not "ready" either.
    no_provider = {k: "x" for k in ("LLM_API_KEY", "DISCORD_BOT_TOKEN", "WHITE_CELL_CHANNEL_ID")}
    assert "AI provider" in missing_required(no_provider)
    assert is_configured(no_provider) is False


def test_the_form_mapping_satisfies_the_cli_loader(tmp_path):
    config = load_config(to_config_mapping(COMPLETE, tmp_path))
    assert config.llm_provider == "gemini"
    assert config.range_dir == tmp_path

    # A stale RANGE_DIR in .env must not send the GUI somewhere else.
    mapping = to_config_mapping({**COMPLETE, "RANGE_DIR": "/somewhere/else"}, tmp_path)
    assert mapping["RANGE_DIR"] == str(tmp_path)

    blank_model = load_config(to_config_mapping({**COMPLETE, "LLM_MODEL": ""}, tmp_path))
    assert blank_model.llm_model == "gemini-2.5-pro"

    with pytest.raises(ConfigError):
        load_config(to_config_mapping({"LLM_PROVIDER": "gemini"}, tmp_path))


def test_model_suggestions_are_per_provider():
    assert "gemini-2.5-pro" in models_for("gemini")
    assert "claude-opus-5" in models_for("anthropic")
    assert models_for("gemini") != models_for("anthropic")
    assert models_for("nonesuch") == ()
