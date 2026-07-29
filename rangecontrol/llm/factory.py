"""Provider selection. Adding a vendor means adding one branch and one module."""

from __future__ import annotations

from rangecontrol.config import Config
from rangecontrol.llm.anthropic_provider import AnthropicProvider
from rangecontrol.llm.base import Provider
from rangecontrol.llm.gemini_provider import GeminiProvider


def _build(provider: str, api_key: str, model: str) -> Provider:
    if provider == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    if provider == "gemini":
        return GeminiProvider(api_key=api_key, model=model)
    raise ValueError(f"Unsupported provider: {provider}")


def build_provider(config: Config) -> Provider:
    """The full-context ruling and generation provider."""
    return _build(config.llm_provider, config.llm_api_key, config.llm_model)


def build_gate_provider(config: Config) -> Provider:
    """The stage-one intent gate provider; may run a cheaper model."""
    return _build(config.llm_provider, config.llm_api_key, config.llm_gate_model)
