from pathlib import Path

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


def test_secrets_are_flagged_for_masking():
    secret = {f.key for f in FIELDS if f.secret}
    assert secret == {"LLM_API_KEY", "DISCORD_BOT_TOKEN"}


def test_gate_model_is_not_a_form_field():
    """Advanced cost lever; surfacing it costs clarity for everyone else."""
    assert "LLM_GATE_MODEL" not in {f.key for f in FIELDS}


def test_missing_required_names_every_gap():
    assert missing_required({}) == (
        "AI provider",
        "API key",
        "Discord bot token",
        "White cell channel ID",
    )


def test_missing_required_is_empty_when_complete():
    assert missing_required(COMPLETE) == ()


def test_whitespace_only_counts_as_missing():
    values = {**COMPLETE, "LLM_API_KEY": "   "}
    assert "API key" in missing_required(values)


def test_is_configured_matches_missing_required():
    assert is_configured(COMPLETE) is True
    assert is_configured({}) is False


def test_models_are_listed_per_provider():
    assert "gemini-2.5-pro" in models_for("gemini")
    assert "claude-opus-5" in models_for("anthropic")
    assert models_for("gemini") != models_for("anthropic")


def test_unknown_provider_yields_no_suggestions():
    assert models_for("nonesuch") == ()


def test_config_mapping_loads_cleanly(tmp_path):
    """The GUI's dict must satisfy the same loader the CLI uses."""
    config = load_config(to_config_mapping(COMPLETE, tmp_path))
    assert config.llm_provider == "gemini"
    assert config.range_dir == tmp_path


def test_config_mapping_forces_range_dir_to_the_app_home(tmp_path):
    """A stale RANGE_DIR in .env must not send the GUI somewhere else."""
    values = {**COMPLETE, "RANGE_DIR": "/somewhere/else"}
    mapping = to_config_mapping(values, tmp_path)
    assert mapping["RANGE_DIR"] == str(tmp_path)


def test_incomplete_mapping_still_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(to_config_mapping({"LLM_PROVIDER": "gemini"}, tmp_path))


def test_blank_model_falls_back_to_the_provider_default(tmp_path):
    values = {**COMPLETE, "LLM_MODEL": ""}
    config = load_config(to_config_mapping(values, tmp_path))
    assert config.llm_model == "gemini-2.5-pro"


def test_required_labels_follow_field_order():
    """Reordering FIELDS for layout must not scramble the message."""
    expected = tuple(f.label for f in FIELDS if f.required)
    assert missing_required({}) == expected


def test_a_blank_provider_is_reported_as_missing():
    """load_config requires it, so is_configured must not say we are ready."""
    values = {k: "x" for k in ("LLM_API_KEY", "DISCORD_BOT_TOKEN", "WHITE_CELL_CHANNEL_ID")}
    assert "AI provider" in missing_required(values)
    assert is_configured(values) is False
