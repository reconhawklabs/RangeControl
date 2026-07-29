from pathlib import Path

import pytest

from rangecontrol.config import Config, ConfigError, load_config

BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "123",
}


def test_loads_minimal_env_with_defaults():
    cfg = load_config(BASE_ENV)
    assert isinstance(cfg, Config)
    assert cfg.discord_bot_token == "tok"
    assert cfg.llm_provider == "anthropic"
    assert cfg.llm_model == "claude-opus-5"
    assert cfg.llm_gate_model == "claude-opus-5"
    assert cfg.white_cell_channel_id == 123
    assert cfg.allowed_channel_ids == ()
    assert cfg.range_dir == Path(".")


def test_gemini_default_model():
    env = {**BASE_ENV, "LLM_PROVIDER": "gemini"}
    assert load_config(env).llm_model == "gemini-2.5-pro"


def test_gate_model_override_does_not_change_main_model():
    env = {**BASE_ENV, "LLM_GATE_MODEL": "claude-haiku-4-5"}
    cfg = load_config(env)
    assert cfg.llm_gate_model == "claude-haiku-4-5"
    assert cfg.llm_model == "claude-opus-5"


def test_parses_allowed_channel_list():
    env = {**BASE_ENV, "ALLOWED_CHANNEL_IDS": "1, 2 ,3"}
    assert load_config(env).allowed_channel_ids == (1, 2, 3)


def test_config_is_frozen():
    cfg = load_config(BASE_ENV)
    with pytest.raises(Exception):
        cfg.llm_model = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "missing", ["DISCORD_BOT_TOKEN", "LLM_PROVIDER", "LLM_API_KEY", "WHITE_CELL_CHANNEL_ID"]
)
def test_missing_required_value_raises(missing):
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert missing in str(exc.value)


def test_unknown_provider_raises_and_lists_valid_options():
    env = {**BASE_ENV, "LLM_PROVIDER": "openai"}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "anthropic" in str(exc.value)
    assert "gemini" in str(exc.value)


def test_non_numeric_channel_id_raises():
    env = {**BASE_ENV, "WHITE_CELL_CHANNEL_ID": "not-a-number"}
    with pytest.raises(ConfigError):
        load_config(env)


def test_non_numeric_allowed_channel_id_raises():
    env = {**BASE_ENV, "ALLOWED_CHANNEL_IDS": "1,nope"}
    with pytest.raises(ConfigError):
        load_config(env)


def test_error_message_never_contains_secret_values():
    env = {**BASE_ENV, "WHITE_CELL_CHANNEL_ID": "bad"}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "tok" not in str(exc.value)
    assert "key" not in str(exc.value)


def test_human_in_the_loop_defaults_to_off():
    """The bot must answer automatically unless someone opted in."""
    config = load_config(BASE_ENV)
    assert config.human_in_the_loop is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_human_in_the_loop_accepts_common_truthy_spellings(raw):
    assert load_config({**BASE_ENV, "HUMAN_IN_THE_LOOP": raw}).human_in_the_loop


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "   "])
def test_human_in_the_loop_accepts_common_falsy_spellings(raw):
    assert not load_config({**BASE_ENV, "HUMAN_IN_THE_LOOP": raw}).human_in_the_loop


def test_an_unparseable_human_in_the_loop_value_is_rejected():
    """Silently reading "maybe" as off would leave rulings unreviewed."""
    with pytest.raises(ConfigError, match="HUMAN_IN_THE_LOOP"):
        load_config({**BASE_ENV, "HUMAN_IN_THE_LOOP": "maybe"})
