import json

import pytest

from rangecontrol.advisor.gate import classify
from rangecontrol.advisor.models import GateVerdict
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider


def reply(verdict, reason="because"):
    return json.dumps({"verdict": verdict, "reason": reason})


def test_returns_parsed_verdict():
    provider = StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)])
    result = classify("can we block 77.232.11.2", provider)
    assert result.verdict == GateVerdict.CHANGE_REQUEST
    assert result.reason == "because"


def test_sends_the_gate_schema():
    provider = StubProvider(completions=[reply(GateVerdict.AMBIGUOUS)])
    classify("q", provider)
    assert provider.calls[0]["schema"]["properties"]["verdict"]["enum"]


def test_question_is_the_only_user_content():
    provider = StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)])
    classify("can we block it", provider)
    assert provider.calls[0]["user"].strip().endswith("can we block it")


def test_no_range_context_reaches_the_gate():
    provider = StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)])
    classify("q", provider)
    call = provider.calls[0]
    assert "RANGE REFERENCE DOCUMENT" not in call["system"]
    assert "RANGE REFERENCE DOCUMENT" not in call["user"]


def test_tolerates_json_wrapped_in_a_code_fence():
    fenced = "```json\n" + reply(GateVerdict.NOT_A_CHANGE_REQUEST) + "\n```"
    provider = StubProvider(completions=[fenced])
    assert classify("q", provider).verdict == GateVerdict.NOT_A_CHANGE_REQUEST


def test_tolerates_a_single_line_code_fence():
    fenced = "```json " + reply(GateVerdict.CHANGE_REQUEST) + "```"
    provider = StubProvider(completions=[fenced])
    assert classify("q", provider).verdict == GateVerdict.CHANGE_REQUEST


def test_backtick_inside_a_string_value_is_preserved():
    payload = json.dumps({"verdict": GateVerdict.AMBIGUOUS, "reason": "uses `code` ticks"})
    provider = StubProvider(completions=[payload])
    assert classify("q", provider).reason == "uses `code` ticks"


def test_malformed_json_raises():
    provider = StubProvider(completions=["not json at all"])
    with pytest.raises(LLMError):
        classify("q", provider)


def test_unknown_verdict_raises():
    provider = StubProvider(completions=[reply("MAYBE")])
    with pytest.raises(LLMError):
        classify("q", provider)


def test_missing_verdict_key_raises():
    provider = StubProvider(completions=[json.dumps({"reason": "x"})])
    with pytest.raises(LLMError):
        classify("q", provider)


def test_json_array_response_raises():
    provider = StubProvider(completions=["[1, 2, 3]"])
    with pytest.raises(LLMError):
        classify("q", provider)


def test_provider_failure_propagates():
    provider = StubProvider(error=LLMError("timeout"))
    with pytest.raises(LLMError):
        classify("q", provider)


def test_blank_question_raises_before_calling_the_model():
    provider = StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)])
    with pytest.raises(LLMError):
        classify("   ", provider)
    assert provider.calls == []
