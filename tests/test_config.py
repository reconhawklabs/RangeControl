from pathlib import Path

import pytest

from rangecontrol.config import DEFAULT_HITL_DENIED_TEXT, ConfigError, load_config

BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "123",
}


def test_minimal_env_loads_with_the_provider_default_models():
    cfg = load_config(BASE_ENV)
    assert cfg.llm_model == "claude-opus-5"
    assert cfg.llm_gate_model == "claude-opus-5"
    assert cfg.white_cell_channel_id == 123
    assert cfg.allowed_channel_ids == ()
    assert cfg.range_dir == Path(".")

    assert load_config({**BASE_ENV, "LLM_PROVIDER": "gemini"}).llm_model == "gemini-2.5-pro"

    gated = load_config({**BASE_ENV, "LLM_GATE_MODEL": "claude-haiku-4-5"})
    assert gated.llm_gate_model == "claude-haiku-4-5"
    assert gated.llm_model == "claude-opus-5"


def test_each_missing_required_value_is_named_in_the_error():
    for missing in BASE_ENV:
        env = {k: v for k, v in BASE_ENV.items() if k != missing}
        with pytest.raises(ConfigError, match=missing):
            load_config(env)
        with pytest.raises(ConfigError, match=missing):
            load_config({**env, missing: "   "})


def test_an_unknown_provider_is_rejected_listing_the_valid_options():
    with pytest.raises(ConfigError) as exc:
        load_config({**BASE_ENV, "LLM_PROVIDER": "openai"})
    assert "anthropic" in str(exc.value)
    assert "gemini" in str(exc.value)


def test_parses_the_allowed_channel_list():
    env = {**BASE_ENV, "ALLOWED_CHANNEL_IDS": "1, 2 ,3"}
    assert load_config(env).allowed_channel_ids == (1, 2, 3)


def test_non_numeric_channel_ids_are_rejected_without_echoing_secrets():
    secrets = {"DISCORD_BOT_TOKEN": "s3cret-bot-token", "LLM_API_KEY": "s3cret-api-key"}
    for name, value in (("WHITE_CELL_CHANNEL_ID", "not-a-number"),
                        ("ALLOWED_CHANNEL_IDS", "1,nope")):
        with pytest.raises(ConfigError, match=name) as exc:
            load_config({**BASE_ENV, **secrets, name: value})
        for secret in secrets.values():
            assert secret not in str(exc.value)


def test_human_in_the_loop_defaults_off_and_reads_common_spellings():
    """The bot answers automatically unless someone opted in."""
    assert load_config(BASE_ENV).human_in_the_loop is False
    for raw in ("true", "TRUE", "1", "yes", "on"):
        assert load_config({**BASE_ENV, "HUMAN_IN_THE_LOOP": raw}).human_in_the_loop is True
    for raw in ("false", "0", "no", "off", "", "   "):
        assert load_config({**BASE_ENV, "HUMAN_IN_THE_LOOP": raw}).human_in_the_loop is False


def test_an_unparseable_human_in_the_loop_value_is_rejected():
    """Silently reading "maybe" as off would leave rulings unreviewed."""
    with pytest.raises(ConfigError, match="HUMAN_IN_THE_LOOP"):
        load_config({**BASE_ENV, "HUMAN_IN_THE_LOOP": "maybe"})


def test_denied_reply_defaults_to_the_built_in_text_and_can_be_customised():
    """An empty denial cannot be sent, so blank means the built-in wording."""
    assert load_config(BASE_ENV).hitl_denied_text == DEFAULT_HITL_DENIED_TEXT
    blank = {**BASE_ENV, "HITL_DENIED_TEXT": "   "}
    assert load_config(blank).hitl_denied_text == DEFAULT_HITL_DENIED_TEXT
    custom = {**BASE_ENV, "HITL_DENIED_TEXT": "  Not at this time.  "}
    assert load_config(custom).hitl_denied_text == "Not at this time."
