import json
import time

import pytest

from rangecontrol.advisor.gate import classify, parse_json_object
from rangecontrol.advisor.models import GateVerdict
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider


def reply(verdict, reason="because"):
    return json.dumps({"verdict": verdict, "reason": reason})


def test_returns_the_verdict_for_the_question_alone():
    """The question is the gate's only user content; no range context rides along."""
    provider = StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)])
    result = classify("can we block 77.232.11.2", provider)
    assert result.verdict == GateVerdict.CHANGE_REQUEST
    assert result.reason == "because"
    call = provider.calls[0]
    assert call["user"].strip().endswith("can we block 77.232.11.2")
    assert "RANGE REFERENCE DOCUMENT" not in call["system"] + call["user"]


def test_tolerates_json_wrapped_in_a_code_fence():
    """In the ruling path a spurious parse failure is a refusal shown to the
    team: a fence with or without a newline must parse, and backticks inside a
    value must survive."""
    cases = (
        (
            "```json\n" + reply(GateVerdict.NOT_A_CHANGE_REQUEST) + "\n```",
            (GateVerdict.NOT_A_CHANGE_REQUEST, "because"),
        ),
        (
            "```json " + reply(GateVerdict.CHANGE_REQUEST) + "```",
            (GateVerdict.CHANGE_REQUEST, "because"),
        ),
        (
            reply(GateVerdict.AMBIGUOUS, "uses `code` ticks"),
            (GateVerdict.AMBIGUOUS, "uses `code` ticks"),
        ),
    )
    wrong = []
    for raw, expected in cases:
        result = classify("q", StubProvider(completions=[raw]))
        if (result.verdict, result.reason) != expected:
            wrong.append((raw, result))
    assert not wrong, wrong


def test_malformed_output_raises():
    """The engine fails closed on a raise, so anything short of a known
    verdict in a JSON object must raise rather than be guessed at."""
    malformed = (
        "not json at all",
        "[1, 2, 3]",
        reply("MAYBE"),
        json.dumps({"reason": "x"}),
    )
    accepted = []
    for raw in malformed:
        try:
            classify("q", StubProvider(completions=[raw]))
        except LLMError:
            continue
        accepted.append(raw)
    assert not accepted, accepted


def test_blank_question_raises_before_calling_the_model():
    provider = StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)])
    with pytest.raises(LLMError):
        classify("   ", provider)
    assert provider.calls == []


def test_fence_stripping_is_linear_on_a_whitespace_run():
    """Every gate and reply-review output passes through parse_json_object.
    The old closing-fence pattern took seconds on a few tens of kilobytes of
    blank space, quadratic in its length."""
    raw = "```json\n{}" + " " * 200_000 + "x"
    started = time.monotonic()
    with pytest.raises(LLMError):
        parse_json_object(raw)
    assert time.monotonic() - started < 2
