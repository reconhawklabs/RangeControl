"""Listing models must never reach the network in tests.

Every case here injects a fake client. The live path is exercised only by an
operator clicking Fetch — it is a metadata call, not a generation, but it is
still a call and this suite makes none.
"""

import types

import pytest

from rangecontrol.config import load_config
from rangecontrol.llm.base import LLMError
from rangecontrol.llm.catalog import list_models

BASE = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "1",
}


def config_for(provider):
    return load_config({**BASE, "LLM_PROVIDER": provider})


class FakeAnthropic:
    def __init__(self, ids=(), error=None):
        self.models = types.SimpleNamespace(list=self._list)
        self._ids = ids
        self._error = error

    def _list(self, **kwargs):
        if self._error:
            raise self._error
        return [types.SimpleNamespace(id=i) for i in self._ids]


class FakeGemini:
    def __init__(self, entries=(), error=None):
        self.models = types.SimpleNamespace(list=self._list)
        self._entries = entries
        self._error = error

    def _list(self):
        if self._error:
            raise self._error
        return [
            types.SimpleNamespace(name=name, supported_actions=actions)
            for name, actions in self._entries
        ]


def test_gemini_listing_offers_only_models_that_can_adjudicate():
    """The API reports "models/x" but accepts only "x". An embedding or image
    model in the dropdown is a trap, not a choice; an absent action list must
    not silently drop a usable model."""
    client = FakeGemini(entries=[
        ("models/gemini-2.5-pro", ["generateContent"]),
        ("models/gemini-9-flash", None),
        ("models/text-bison", ["countTokens"]),
        *[(f"models/{name}", ["generateContent"])
          for name in ("text-embedding-004", "gemini-embedding-1", "imagen-3", "veo-2", "aqa")],
    ])
    assert list_models(config_for("gemini"), client) == ("gemini-9-flash", "gemini-2.5-pro")


def test_anthropic_listing_is_deduplicated_newest_looking_first():
    client = FakeAnthropic(
        ids=["claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-5"]
    )
    assert list_models(config_for("anthropic"), client) == (
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-haiku-4-5",
    )


def test_failures_and_empty_results_are_llm_errors_not_empty_dropdowns():
    """A silently empty list reads as "no models exist", which is never true."""
    with pytest.raises(LLMError, match="Anthropic"):
        list_models(config_for("anthropic"), FakeAnthropic(error=RuntimeError("401")))
    with pytest.raises(LLMError, match="Gemini"):
        list_models(config_for("gemini"), FakeGemini(error=RuntimeError("bad key")))
    with pytest.raises(LLMError, match="no usable models"):
        list_models(config_for("anthropic"), FakeAnthropic(ids=[]))
