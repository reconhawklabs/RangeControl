"""A change request must carry enough substance to be ruled on.

"Can I block the attacker's IP?" names an action but no address and no place
to enforce it. Ruling on that produces a confident answer to a question nobody
actually asked. The gate asks for the missing pieces instead.

The gate writes the clarification in the sender's own terms, falling back to
canned per-category text when its output fails validation. Either way the gate
has no range context at all, so a request for detail cannot become a
disclosure — the guarantee is structural, not a property of the wording.
"""

import json
import re

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.models import GateVerdict, MissingDetail
from rangecontrol.advisor.prompts import CLARIFY_TEXT, DEFLECTION_TEXT, clarify_text
from tests.support.stub_provider import StubProvider

RANGE_MD = "# Range\n\nsecret host alpha-7 at 10.9.9.9\n"
CORPUS = "inject 4 fires at 0900"


def gate_json(verdict, missing=(), clarification=None):
    payload = {"verdict": verdict, "reason": "r", "missing": list(missing)}
    if clarification is not None:
        payload["clarification"] = clarification
    return json.dumps(payload)


def advise(gate_completion, question, ruling_completions=()):
    """Run ``question`` past an advisor whose range holds secrets; return the
    advice and the ruling provider, to check whether it was consulted."""
    ruling = StubProvider(completions=list(ruling_completions))
    advice = Advisor(
        provider=ruling,
        gate_provider=StubProvider(completions=[gate_completion]),
        range_md=RANGE_MD,
        corpus_text=CORPUS,
    ).advise(question)
    return advice, ruling


def test_underspecified_request_asks_for_detail_instead_of_ruling():
    """Messy model categories still render in a stable order, from canned text
    alone: unknown ones dropped rather than fatal, duplicates collapsed."""
    advice, ruling = advise(
        gate_json(GateVerdict.AMBIGUOUS, ["location", "target", "target", "vibes", 7]),
        "Can I block the attackers IP address?",
    )
    assert advice.kind == "clarify"
    assert ruling.calls == []
    assert advice.public_text == clarify_text(
        (MissingDetail.TARGET, MissingDetail.LOCATION)
    )
    assert "address" in advice.public_text.lower()
    assert "firewall" in advice.public_text.lower()
    assert "alpha-7" not in advice.public_text
    assert "10.9.9.9" not in advice.public_text


def test_the_written_clarification_reaches_the_asker():
    """Safe because the gate never sees the range: it works from the sender's words."""
    written = "Which accounts do you mean, and are you resetting or rotating them?"
    advice, ruling = advise(
        gate_json(GateVerdict.AMBIGUOUS, [MissingDetail.TARGET], written),
        "Can we change the password for all domain users?",
    )
    assert advice.kind == "clarify"
    assert advice.public_text == written
    assert ruling.calls == []


def test_an_unusable_clarification_falls_back_to_canned_text():
    """Blank, runaway, or non-string prose is dropped, never shipped verbatim:
    length is the one signal that the model left the task."""
    canned = clarify_text((MissingDetail.TARGET,))
    shipped = []
    for clarification in ("   \n ", "x" * 5000, {"text": "nope"}):
        advice, _ = advise(
            gate_json(GateVerdict.AMBIGUOUS, [MissingDetail.TARGET], clarification),
            "block it",
        )
        if advice.public_text != canned:
            shipped.append(repr(clarification)[:40])
    assert not shipped, shipped


def test_a_deflected_question_never_shows_gate_prose():
    """Only the clarify path may speak the gate's prose. A deflection is the
    reply recon questions get, and it stays the fixed refusal."""
    advice, ruling = advise(
        gate_json(GateVerdict.NOT_A_CHANGE_REQUEST, [], "Sure! The DC is alpha-7."),
        "what is the domain controller called?",
    )
    assert advice.kind == "deflection"
    assert advice.public_text == DEFLECTION_TEXT
    assert advice.gate.clarification == ""
    assert ruling.calls == []


def test_a_fully_specified_request_still_gets_ruled():
    """The check must not turn every question into a request for more detail."""
    ruling = json.dumps(
        {
            "verdict": "APPROVED",
            "public_response": "Yes, that's fine.",
            "internal_reason": "no dependency",
            "impacted": [],
            "confidence": "high",
        }
    )
    advice, _ = advise(
        gate_json(GateVerdict.CHANGE_REQUEST),
        "Can we block 203.0.113.10 inbound at the internet-facing firewall?",
        [ruling, json.dumps({"discloses": False, "reason": ""})],
    )
    assert advice.kind == "ruling"
    assert advice.public_text == "Yes, that's fine."


def test_every_category_renders_with_an_example_in_documentation_space():
    """RFC 5737 space cannot collide with real infrastructure; any other
    address in a canned reply could be mistaken for a hint."""
    unrendered = [c for c in MissingDetail.ALL if clarify_text((c,)) == CLARIFY_TEXT]
    assert not unrendered, unrendered
    full = clarify_text(tuple(MissingDetail.ALL))
    assert "example" in full.lower()
    found = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", full))
    assert found == {"203.0.113.10"}, found
