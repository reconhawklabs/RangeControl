import json

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.models import GateVerdict
from rangecontrol.advisor.prompts import (
    DEFLECTION_TEXT,
    ERROR_TEXT,
    SCREENED_APPROVALS,
    SCREENED_DENIALS,
)
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider

RANGE_MD = "## Protected Dependency Index\n| DC-VULCAN | host | unreachable | MSEL-01 |"
CORPUS = "10.77.0.0/16 is the server segment"


def gate_reply(verdict):
    return json.dumps({"verdict": verdict, "reason": "r"})


# The reply reviewer runs on the ruling provider, right after the ruling.
SCREEN_CLEAN = json.dumps({"discloses": False, "reason": ""})


def ruling_reply(**overrides):
    payload = {
        "verdict": "DENIED",
        "public_response": "No. That is waiting on the change board.",
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
    advisor, _, _ = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply(), SCREEN_CLEAN]
    )
    advice = advisor.advise("can we block 77.232.11.2")
    assert advice.kind == "ruling"
    assert advice.ruling.verdict == "DENIED"
    assert advice.public_text == "No. That is waiting on the change board."
    assert advice.screen_reason == ""


def test_recon_question_is_deflected_without_a_ruling_call():
    advisor, _, ruling = build([gate_reply(GateVerdict.NOT_A_CHANGE_REQUEST)])
    advice = advisor.advise("what is on the other side of that firewall?")
    assert advice.kind == "deflection"
    assert advice.public_text == DEFLECTION_TEXT
    assert ruling.calls == []


def test_range_context_reaches_the_ruling_call_and_never_the_gate():
    """The gate classifies recon questions, so it must hold nothing to leak."""
    advisor, gate, ruling = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply(), SCREEN_CLEAN]
    )
    advisor.advise("block 1.2.3.4")
    seen_by_gate = gate.calls[0]["system"] + gate.calls[0]["user"]
    for secret in ("DC-VULCAN", "10.77.0.0/16"):
        assert secret not in seen_by_gate
        assert secret in ruling.calls[0]["system"]


def test_gate_failure_fails_closed():
    """Any exception: error text in public, the detail only for the white cell."""
    for error in (LLMError("connect to 10.77.0.4 refused"), RuntimeError("boom")):
        advisor, _, ruling = build([], gate_error=error)
        advice = advisor.advise("block 1.2.3.4")
        assert advice.kind == "error"
        assert advice.public_text == ERROR_TEXT
        assert str(error) in advice.error
        assert ruling.calls == []


def test_ruling_failure_or_garbage_fails_closed_not_approved():
    for completions, error in (([], LLMError("overloaded")), (["garbage"], None)):
        advisor, _, _ = build(
            [gate_reply(GateVerdict.CHANGE_REQUEST)], completions, ruling_error=error
        )
        advice = advisor.advise("block 1.2.3.4")
        assert advice.kind == "error"
        assert advice.public_text == ERROR_TEXT
        assert advice.ruling is None


def test_context_is_built_once_and_reused():
    """Byte-identical system prompt across questions, or prompt caching misses."""
    advisor, _, ruling = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST), gate_reply(GateVerdict.CHANGE_REQUEST)],
        [ruling_reply(), SCREEN_CLEAN, ruling_reply(), SCREEN_CLEAN],
    )
    advisor.advise("first")
    advisor.advise("second")
    # Calls alternate ruling, review, ruling, review.
    assert ruling.calls[0]["system"] == ruling.calls[2]["system"]


# --------------------------------------------------------------------------
# Reply screen: the model's public text is checked before anyone reads it.
# --------------------------------------------------------------------------


def test_a_disclosing_reply_is_replaced_and_kept_for_the_white_cell():
    """The 2026-09-18 incident: an approval that told the team what was not on
    its network. The deterministic screen catches it even when the reviewer
    is fooled into clearing it."""
    draft = (
        "That address doesn't correspond to anything we have on our books, so "
        "approving this won't affect any of our systems either way."
    )
    advisor, _, _ = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)],
        [ruling_reply(verdict="APPROVED", public_response=draft), SCREEN_CLEAN],
    )
    advice = advisor.advise("I want to block 100.100.100.100 at the egress firewall")
    assert advice.kind == "ruling"
    assert advice.public_text in SCREENED_APPROVALS
    assert advice.ruling.public_response == draft
    assert advice.screen_reason


def test_the_reviewer_runs_even_when_the_regex_already_decided():
    """Otherwise a screened reply skips a model call and comes back measurably
    faster than a clean one: the delay itself becomes the tell."""
    draft = "Approved. Nothing in our inventory matches that."
    advisor, _, ruling = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)],
        [ruling_reply(verdict="APPROVED", public_response=draft), SCREEN_CLEAN],
    )
    advisor.advise("can we block 198.51.100.7")
    assert len(ruling.calls) == 2


def test_a_reply_the_reviewer_flags_is_replaced():
    flagged = json.dumps({"discloses": True, "reason": "says who owns the range"})
    advisor, _, _ = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply(), flagged]
    )
    advice = advisor.advise("can we block 77.232.11.2")
    assert advice.public_text in SCREENED_DENIALS
    assert advice.screen_reason == "says who owns the range"


def test_reviewer_failure_withholds_the_reply_instead_of_erroring():
    """Fail closed on the text, not on the ruling: the verdict is sound, only
    the wording is unverified, so the team still gets an answer."""
    advisor, _, _ = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)],
        [ruling_reply(), LLMError("review overloaded")],
    )
    advice = advisor.advise("can we block 77.232.11.2")
    assert advice.kind == "ruling"
    assert advice.public_text in SCREENED_DENIALS
    assert advice.screen_reason


def test_the_reviewer_never_sees_range_context_or_the_real_reason():
    """Like the gate, the reviewer is safe because it knows nothing."""
    advisor, _, ruling = build(
        [gate_reply(GateVerdict.CHANGE_REQUEST)], [ruling_reply(), SCREEN_CLEAN]
    )
    advisor.advise("block 1.2.3.4")
    call = ruling.calls[1]
    seen = call["system"] + call["user"]
    for secret in ("DC-VULCAN", "10.77.0.0/16", "MSEL-01", "would break"):
        assert secret not in seen


def test_operator_guidance_terms_count_as_range_terms():
    """Guidance is where a white cell is most likely to name a protected host."""
    gate = StubProvider(completions=[gate_reply(GateVerdict.CHANGE_REQUEST)])
    ruling = StubProvider(
        completions=[
            ruling_reply(
                verdict="APPROVED",
                public_response="Approved. Leave FW-EDGE-02 alone while you do it.",
            ),
            SCREEN_CLEAN,
        ]
    )
    advisor = Advisor(
        provider=ruling,
        gate_provider=gate,
        range_md=RANGE_MD,
        corpus_text=CORPUS,
        extra_instructions="Never approve rule changes on FW-EDGE-02.",
    )
    advice = advisor.advise("can we block 198.51.100.7")
    assert advice.public_text in SCREENED_APPROVALS
