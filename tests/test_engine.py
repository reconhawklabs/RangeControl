import json

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.models import GateVerdict
from rangecontrol.advisor.prompts import CLARIFY_TEXT, DEFLECTION_TEXT, ERROR_TEXT
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider

RANGE_MD = "## Protected Dependency Index\n| DC-VULCAN | host | unreachable | MSEL-01 |"
CORPUS = "10.77.0.0/16 is the server segment"


def gate_reply(verdict):
    return json.dumps({"verdict": verdict, "reason": "r"})


def ruling_reply(**overrides):
    payload = {
        "verdict": "DENIED",
        "public_response": "No. That range belongs to a partner service.",
        "internal_reason": "would break MSEL-01 callback to DC-VULCAN",
        "impacted": ["MSEL-01"],
        "confidence": "high",
    }
    payload.update(overrides)
    return json.dumps(payload)


def build(gate_completions, ruling_completions=None, gate_error=None, ruling_error=None):
    gate = StubProvider(completions=gate_completions, error=gate_error)
    ruling = StubProvider(completions=ruling_completions or [], error=ruling_error)
    advisor = Advisor(
        provider=ruling, gate_provider=gate, range_md=RANGE_MD, corpus_text=CORPUS
    )
    return advisor, gate, ruling


def test_change_request_produces_a_ruling():
    advisor, _, _ = build([gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply()])
    advice = advisor.advise("can we block 77.232.11.2")
    assert advice.kind == "ruling"
    assert advice.ruling.verdict == "DENIED"
    assert advice.public_text.startswith("No.")


def test_recon_question_is_deflected_without_a_ruling_call():
    advisor, _, ruling = build([gate_reply(GateVerdict.NOT_A_CHANGE_REQUEST)])
    advice = advisor.advise("what is on the other side of that firewall?")
    assert advice.kind == "deflection"
    assert advice.public_text == DEFLECTION_TEXT
    assert ruling.calls == []


def test_ambiguous_question_asks_for_a_restatement():
    advisor, _, ruling = build([gate_reply(GateVerdict.AMBIGUOUS)])
    advice = advisor.advise("can we change something")
    assert advice.kind == "clarify"
    assert advice.public_text == CLARIFY_TEXT
    assert ruling.calls == []


def test_range_context_never_reaches_the_gate():
    advisor, gate, _ = build([gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply()])
    advisor.advise("block 1.2.3.4")
    call = gate.calls[0]
    assert "DC-VULCAN" not in call["system"] + call["user"]
    assert "10.77.0.0/16" not in call["system"] + call["user"]


def test_range_context_does_reach_the_ruling_call():
    advisor, _, ruling = build([gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply()])
    advisor.advise("block 1.2.3.4")
    assert "DC-VULCAN" in ruling.calls[0]["system"]


def test_gate_failure_fails_closed():
    advisor, _, ruling = build([], gate_error=LLMError("gate down"))
    advice = advisor.advise("block 1.2.3.4")
    assert advice.kind == "error"
    assert advice.public_text == ERROR_TEXT
    assert advice.error
    assert ruling.calls == []


def test_ruling_failure_fails_closed():
    advisor, _, _ = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)], ruling_error=LLMError("overloaded")
    )
    advice = advisor.advise("block 1.2.3.4")
    assert advice.kind == "error"
    assert advice.public_text == ERROR_TEXT


def test_malformed_ruling_fails_closed_not_approved():
    advisor, _, _ = build([gate_reply(GateVerdict.CHANGE_REQUEST)], ["garbage"])
    advice = advisor.advise("block 1.2.3.4")
    assert advice.kind == "error"
    assert advice.ruling is None


def test_unexpected_exception_fails_closed():
    advisor, _, _ = build([], gate_error=RuntimeError("boom"))
    assert advisor.advise("q").kind == "error"


def test_error_text_never_leaks_internal_detail():
    advisor, _, _ = build([], gate_error=LLMError("connect to 10.77.0.4 refused"))
    advice = advisor.advise("q")
    assert "10.77.0.4" not in advice.public_text
    assert "10.77.0.4" in advice.error


def test_blank_question_is_clarified_without_any_model_call():
    advisor, gate, ruling = build([])
    advice = advisor.advise("   ")
    assert advice.kind == "clarify"
    assert gate.calls == []
    assert ruling.calls == []


def test_context_is_built_once_and_reused():
    advisor, _, ruling = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST), gate_reply(GateVerdict.CHANGE_REQUEST)],
        [ruling_reply(), ruling_reply()],
    )
    advisor.advise("first")
    advisor.advise("second")
    assert ruling.calls[0]["system"] == ruling.calls[1]["system"]
