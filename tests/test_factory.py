from rangecontrol.config import load_config
from rangecontrol.llm.anthropic_provider import AnthropicProvider
from rangecontrol.llm.factory import build_gate_provider, build_provider
from rangecontrol.llm.gemini_provider import GeminiProvider

BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "1",
}


def test_builds_anthropic_provider():
    cfg = load_config({**BASE_ENV, "LLM_PROVIDER": "anthropic"})
    assert isinstance(build_provider(cfg), AnthropicProvider)


def test_builds_gemini_provider():
    cfg = load_config({**BASE_ENV, "LLM_PROVIDER": "gemini"})
    assert isinstance(build_provider(cfg), GeminiProvider)


def test_gate_provider_uses_gate_model():
    cfg = load_config(
        {**BASE_ENV, "LLM_PROVIDER": "anthropic", "LLM_GATE_MODEL": "claude-haiku-4-5"}
    )
    assert build_gate_provider(cfg)._model == "claude-haiku-4-5"
    assert build_provider(cfg)._model == "claude-opus-5"
