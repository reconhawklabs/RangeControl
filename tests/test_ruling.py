import json

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


def accepted(raws):
    """Every raw ruling in ``raws`` that adjudicate returns instead of raising."""
    passed = []
    for raw in raws:
        try:
            adjudicate("q", "CONTEXT", StubProvider(completions=[raw]))
        except LLMError:
            continue
        passed.append(raw)
    return passed


def test_public_and_internal_fields_are_not_conflated():
    """The whole project exists to keep these apart."""
    provider = StubProvider(
        completions=[
            reply(
                verdict="DENIED",
                public_response="No. That is waiting on the change board.",
                internal_reason="would break MSEL-14 callback to DC-VULCAN",
                impacted=["MSEL-14", "path-b"],
            )
        ]
    )
    ruling = adjudicate("q", "CONTEXT", provider)
    assert ruling.verdict == "DENIED"
    assert ruling.public_response == "No. That is waiting on the change board."
    assert ruling.internal_reason == "would break MSEL-14 callback to DC-VULCAN"
    assert ruling.impacted == ("MSEL-14", "path-b")


def test_text_fields_are_type_checked_not_coerced():
    """str(None) is "None": non-empty, and it would reach the team as a ruling."""
    passed = accepted(
        (
            reply(public_response=None),
            reply(public_response="  "),
            reply(internal_reason=None),
        )
    )
    assert not passed, passed


def test_malformed_ruling_output_raises():
    """The engine turns the raise into the error text, never an approval."""
    passed = accepted(
        (
            "I think that's fine!",
            reply(verdict="MAYBE"),
            reply(confidence="certain"),
            reply(impacted="MSEL-14"),
            reply(impacted=[{"id": "MSEL-14"}]),
        )
    )
    assert not passed, passed


def test_context_is_the_system_prompt_byte_for_byte_on_every_call():
    """The cached prefix holds only if the question stays in the user turn."""
    provider = StubProvider(completions=[reply(), reply()])
    adjudicate("first", "STANDING CONTEXT", provider)
    adjudicate("can we block it", "STANDING CONTEXT", provider)
    assert [call["system"] for call in provider.calls] == ["STANDING CONTEXT"] * 2
    assert "can we block it" in provider.calls[1]["user"]
