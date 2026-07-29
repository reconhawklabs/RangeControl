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
        self.kwargs = None

    def _list(self, **kwargs):
        self.kwargs = kwargs
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


# --- anthropic --------------------------------------------------------------


def test_anthropic_ids_are_returned():
    client = FakeAnthropic(ids=["claude-opus-5", "claude-sonnet-5"])
    assert list_models(config_for("anthropic"), client) == (
        "claude-sonnet-5",
        "claude-opus-5",
    )


def test_anthropic_failure_becomes_an_llm_error():
    client = FakeAnthropic(error=RuntimeError("401"))
    with pytest.raises(LLMError, match="Anthropic"):
        list_models(config_for("anthropic"), client)


# --- gemini -----------------------------------------------------------------


def test_gemini_names_are_stripped_of_their_prefix():
    """The API reports "models/x" but accepts only "x"."""
    client = FakeGemini(entries=[("models/gemini-2.5-pro", ["generateContent"])])
    assert list_models(config_for("gemini"), client) == ("gemini-2.5-pro",)


def test_gemini_models_that_cannot_answer_are_dropped():
    client = FakeGemini(entries=[
        ("models/gemini-2.5-pro", ["generateContent"]),
        ("models/text-bison", ["countTokens"]),
    ])
    assert list_models(config_for("gemini"), client) == ("gemini-2.5-pro",)


def test_gemini_keeps_a_model_whose_actions_are_unreported():
    """An absent action list must not silently drop a usable model."""
    client = FakeGemini(entries=[("models/gemini-9-flash", None)])
    assert list_models(config_for("gemini"), client) == ("gemini-9-flash",)


def test_gemini_failure_becomes_an_llm_error():
    client = FakeGemini(error=RuntimeError("bad key"))
    with pytest.raises(LLMError, match="Gemini"):
        list_models(config_for("gemini"), client)


# --- shared behaviour -------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["text-embedding-004", "gemini-embedding-1", "imagen-3", "veo-2", "aqa"]
)
def test_models_that_cannot_adjudicate_are_excluded(name):
    """An embedding or image model in the dropdown is a trap, not a choice."""
    client = FakeGemini(entries=[(f"models/{name}", ["generateContent"]),
                                 ("models/gemini-2.5-pro", ["generateContent"])])
    assert list_models(config_for("gemini"), client) == ("gemini-2.5-pro",)


def test_duplicates_are_collapsed():
    client = FakeAnthropic(ids=["claude-opus-5", "claude-opus-5"])
    assert list_models(config_for("anthropic"), client) == ("claude-opus-5",)


def test_an_empty_result_is_an_error_not_an_empty_dropdown():
    """A silently empty list reads as "no models exist", which is never true."""
    client = FakeAnthropic(ids=[])
    with pytest.raises(LLMError, match="no usable models"):
        list_models(config_for("anthropic"), client)


def test_newest_looking_first():
    client = FakeAnthropic(ids=["claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5"])
    assert list_models(config_for("anthropic"), client)[0] == "claude-sonnet-5"
