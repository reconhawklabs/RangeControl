import json

import pytest

from rangecontrol.advisor.ruling import adjudicate
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider


def reply(**overrides):
    payload = {
        "verdict": "APPROVED",
        "public_response": "Approved. Stage it during a window you can back out of.",
        "internal_reason": "no dependency touched",
        "impacted": [],
        "confidence": "high",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_returns_parsed_ruling():
    provider = StubProvider(completions=[reply()])
    ruling = adjudicate("block 1.2.3.4", "CONTEXT", provider)
    assert ruling.verdict == "APPROVED"
    assert ruling.confidence == "high"
    assert ruling.impacted == ()


def test_public_and_internal_fields_are_not_conflated():
    provider = StubProvider(
        completions=[
            reply(
                public_response="No. A partner service depends on that path.",
                internal_reason="would break MSEL-14 callback to DC-VULCAN",
            )
        ]
    )
    ruling = adjudicate("q", "CONTEXT", provider)
    assert ruling.public_response == "No. A partner service depends on that path."
    assert ruling.internal_reason == "would break MSEL-14 callback to DC-VULCAN"
    # The whole project exists to keep these apart.
    assert "MSEL-14" not in ruling.public_response
    assert "DC-VULCAN" not in ruling.public_response


def test_null_public_response_raises():
    provider = StubProvider(completions=[reply(public_response=None)])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_null_internal_reason_raises():
    provider = StubProvider(completions=[reply(internal_reason=None)])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_non_string_impacted_item_raises():
    provider = StubProvider(completions=[reply(impacted=[{"id": "MSEL-14"}])])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_impacted_is_a_tuple_of_strings():
    provider = StubProvider(completions=[reply(impacted=["MSEL-14", "path-b"])])
    assert adjudicate("q", "CONTEXT", provider).impacted == ("MSEL-14", "path-b")


def test_context_is_sent_as_the_system_prompt():
    provider = StubProvider(completions=[reply()])
    adjudicate("q", "STANDING CONTEXT", provider)
    assert provider.calls[0]["system"] == "STANDING CONTEXT"


def test_question_is_sent_as_the_user_turn():
    provider = StubProvider(completions=[reply()])
    adjudicate("can we block it", "CONTEXT", provider)
    assert "can we block it" in provider.calls[0]["user"]


def test_system_prompt_is_byte_identical_across_calls_for_caching():
    provider = StubProvider(completions=[reply(), reply()])
    adjudicate("first", "CONTEXT", provider)
    adjudicate("second", "CONTEXT", provider)
    assert provider.calls[0]["system"] == provider.calls[1]["system"]


def test_ruling_schema_is_sent():
    provider = StubProvider(completions=[reply()])
    adjudicate("q", "CONTEXT", provider)
    assert set(provider.calls[0]["schema"]["properties"]["verdict"]["enum"]) == {
        "APPROVED",
        "DENIED",
    }


def test_unknown_verdict_raises():
    provider = StubProvider(completions=[reply(verdict="MAYBE")])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_unknown_confidence_raises():
    provider = StubProvider(completions=[reply(confidence="certain")])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_empty_public_response_raises():
    provider = StubProvider(completions=[reply(public_response="  ")])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_non_list_impacted_raises():
    provider = StubProvider(completions=[reply(impacted="MSEL-14")])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_malformed_json_raises():
    provider = StubProvider(completions=["I think that's fine!"])
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)


def test_provider_failure_propagates():
    provider = StubProvider(error=LLMError("overloaded"))
    with pytest.raises(LLMError):
        adjudicate("q", "CONTEXT", provider)
