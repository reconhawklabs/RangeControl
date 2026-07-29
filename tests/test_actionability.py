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

import pytest

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.gate import classify
from rangecontrol.advisor.models import GateVerdict, MissingDetail
from rangecontrol.advisor.prompts import CLARIFY_TEXT, clarify_text
from tests.support.stub_provider import StubProvider


def reply(verdict, missing=(), reason="because"):
    return json.dumps(
        {"verdict": verdict, "reason": reason, "missing": list(missing)}
    )


# --- the gate carries the missing pieces -----------------------------------


def test_gate_reports_which_details_are_missing():
    provider = StubProvider(
        completions=[
            reply(GateVerdict.AMBIGUOUS, [MissingDetail.TARGET, MissingDetail.LOCATION])
        ]
    )
    result = classify("can I block the attackers IP address?", provider)
    assert result.verdict == GateVerdict.AMBIGUOUS
    assert result.missing == (MissingDetail.TARGET, MissingDetail.LOCATION)


def test_missing_defaults_to_empty_when_absent():
    """An older or terser response must not break the gate."""
    provider = StubProvider(
        completions=[json.dumps({"verdict": GateVerdict.CHANGE_REQUEST, "reason": "r"})]
    )
    assert classify("q", provider).missing == ()


def test_unknown_missing_categories_are_dropped_not_fatal():
    """A category we have no canned text for cannot be rendered, so ignore it.

    Raising instead would turn a cosmetic model slip into a refusal.
    """
    provider = StubProvider(
        completions=[reply(GateVerdict.AMBIGUOUS, ["target", "vibes", 7])]
    )
    assert classify("q", provider).missing == (MissingDetail.TARGET,)


def test_duplicate_categories_are_collapsed():
    provider = StubProvider(
        completions=[reply(GateVerdict.AMBIGUOUS, ["target", "target"])]
    )
    assert classify("q", provider).missing == (MissingDetail.TARGET,)


def test_the_schema_offers_the_missing_field():
    provider = StubProvider(completions=[reply(GateVerdict.AMBIGUOUS)])
    classify("q", provider)
    schema = provider.calls[0]["schema"]
    assert "missing" in schema["properties"]
    assert schema["properties"]["missing"]["items"]["enum"]


def test_the_gate_prompt_asks_for_actionability():
    provider = StubProvider(completions=[reply(GateVerdict.AMBIGUOUS)])
    classify("q", provider)
    assert "actionab" in provider.calls[0]["system"].lower()


# --- the composed clarification --------------------------------------------


def test_clarify_text_names_each_missing_piece():
    text = clarify_text((MissingDetail.TARGET, MissingDetail.LOCATION))
    assert "address" in text.lower()
    assert "firewall" in text.lower()


def test_clarify_text_falls_back_when_nothing_is_named():
    assert clarify_text(()) == CLARIFY_TEXT


def test_clarify_text_covers_every_category():
    """Every category the gate can return must render to something."""
    for category in MissingDetail.ALL:
        text = clarify_text((category,))
        assert text != CLARIFY_TEXT, category
        assert len(text) > len(CLARIFY_TEXT) // 2


def test_clarify_text_gives_a_worked_example():
    """'Give me more' is not useful without showing what enough looks like."""
    text = clarify_text((MissingDetail.TARGET,))
    assert "example" in text.lower()


def test_the_only_address_shown_is_documentation_space():
    """RFC 5737 space cannot collide with real infrastructure.

    Any other address in a canned reply would be a constant the blue team
    could mistake for a hint about this environment.
    """
    import re

    full = clarify_text(tuple(MissingDetail.ALL))
    found = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", full))
    assert found == {"203.0.113.10"}, found


def test_the_clarification_is_a_pure_function_of_the_categories():
    """It cannot vary with range content, because it never receives any.

    This is the structural guarantee: clarify_text takes the category list and
    nothing else, so no range document can reach the text a user sees.
    """
    import inspect

    params = list(inspect.signature(clarify_text).parameters)
    assert params == ["missing"]
    assert clarify_text((MissingDetail.TARGET,)) == clarify_text(
        (MissingDetail.TARGET,)
    )


def test_category_order_is_stable_regardless_of_model_output_order():
    """Two identical questions must not produce differently-ordered replies."""
    forward = clarify_text((MissingDetail.TARGET, MissingDetail.LOCATION))
    provider = StubProvider(
        completions=[reply(GateVerdict.AMBIGUOUS, ["location", "target"])]
    )
    assert clarify_text(classify("q", provider).missing) == forward


# --- end to end through the advisor ----------------------------------------


def advisor_for(gate_reply):
    return Advisor(
        provider=StubProvider(completions=[]),
        gate_provider=StubProvider(completions=[gate_reply]),
        range_md="# Range\n\nsecret host alpha-7 at 10.9.9.9\n",
        corpus_text="inject 4 fires at 0900",
    )


def test_underspecified_request_asks_for_detail_instead_of_ruling():
    advice = advisor_for(
        reply(GateVerdict.AMBIGUOUS, [MissingDetail.TARGET, MissingDetail.LOCATION])
    ).advise("Can I block the attackers IP address?")
    assert advice.kind == "clarify"
    assert "address" in advice.public_text.lower()
    assert "firewall" in advice.public_text.lower()


def test_the_clarification_never_reaches_the_ruling_model():
    """No range context is consulted to ask for detail, so none can escape."""
    ruling_provider = StubProvider(completions=[])
    advisor = Advisor(
        provider=ruling_provider,
        gate_provider=StubProvider(
            completions=[reply(GateVerdict.AMBIGUOUS, [MissingDetail.TARGET])]
        ),
        range_md="# Range\n\nhost alpha-7 at 10.9.9.9\n",
        corpus_text="inject 4 fires at 0900",
    )
    advice = advisor.advise("block the bad IP")
    assert ruling_provider.calls == []
    assert "alpha-7" not in advice.public_text
    assert "10.9.9.9" not in advice.public_text


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
    advisor = Advisor(
        provider=StubProvider(completions=[ruling]),
        gate_provider=StubProvider(completions=[reply(GateVerdict.CHANGE_REQUEST)]),
        range_md="# Range",
        corpus_text="",
    )
    advice = advisor.advise(
        "Can we block 203.0.113.10 inbound at the internet-facing firewall?"
    )
    assert advice.kind == "ruling"
    assert advice.public_text == "Yes, that's fine."


# --- the gate writes the clarification itself ------------------------------
#
# A canned per-category list produced "which firewall, router, switch, host, or
# appliance" in response to a question about domain passwords, followed by a
# worked example about blocking an IP. Rigid, and actively misleading.
#
# The gate writes it instead. That is safe for the same structural reason the
# canned text was: the gate has never seen the range document, so its prose
# cannot contain anything from it. It can only work from the sender's words.


def reply_with(clarification, missing=(MissingDetail.TARGET,)):
    return json.dumps(
        {
            "verdict": GateVerdict.AMBIGUOUS,
            "reason": "r",
            "missing": list(missing),
            "clarification": clarification,
        }
    )


def test_gate_clarification_is_used_when_present():
    written = (
        "Which accounts, and where? For example: 'can we force a password "
        "reset for the service accounts on the domain controller?'"
    )
    provider = StubProvider(completions=[reply_with(written)])
    assert classify("change all domain passwords?", provider).clarification == written


def test_the_written_clarification_reaches_the_asker():
    written = "Which accounts do you mean, and are you resetting or rotating them?"
    advice = Advisor(
        provider=StubProvider(completions=[]),
        gate_provider=StubProvider(completions=[reply_with(written)]),
        range_md="# Range\n\nhost alpha-7 at 10.9.9.9\n",
        corpus_text="inject 4 fires at 0900",
    ).advise("Can we change the password for all domain users?")
    assert advice.kind == "clarify"
    assert advice.public_text == written


def test_falls_back_to_canned_text_when_the_gate_writes_nothing():
    """The reply must not be empty just because the model omitted the field."""
    provider = StubProvider(
        completions=[
            json.dumps(
                {
                    "verdict": GateVerdict.AMBIGUOUS,
                    "reason": "r",
                    "missing": [MissingDetail.TARGET],
                }
            )
        ]
    )
    result = classify("q", provider)
    assert result.clarification == ""
    advice = Advisor(
        provider=StubProvider(completions=[]),
        gate_provider=StubProvider(
            completions=[
                json.dumps(
                    {
                        "verdict": GateVerdict.AMBIGUOUS,
                        "reason": "r",
                        "missing": [MissingDetail.TARGET],
                    }
                )
            ]
        ),
        range_md="# Range",
        corpus_text="",
    ).advise("block it")
    assert "address" in advice.public_text.lower()


def test_whitespace_only_clarification_falls_back():
    assert classify("q", StubProvider(completions=[reply_with("   \n ")])).clarification == ""


def test_a_runaway_clarification_falls_back_to_canned_text():
    """Length is the one signal that the model left the task.

    A clarification is two or three sentences. An essay means something went
    wrong, and shipping it verbatim to the team is worse than a generic ask.
    """
    provider = StubProvider(completions=[reply_with("x" * 5000)])
    assert classify("q", provider).clarification == ""


def test_non_string_clarification_is_ignored():
    provider = StubProvider(
        completions=[
            json.dumps(
                {
                    "verdict": GateVerdict.AMBIGUOUS,
                    "reason": "r",
                    "missing": [],
                    "clarification": {"text": "nope"},
                }
            )
        ]
    )
    assert classify("q", provider).clarification == ""


def test_clarification_is_ignored_for_non_ambiguous_verdicts():
    """Only the clarify path may speak the gate's prose.

    A deflection must stay the fixed refusal: it is the reply recon questions
    get, and it is the one place a model must not be given room to improvise.
    """
    provider = StubProvider(
        completions=[
            json.dumps(
                {
                    "verdict": GateVerdict.NOT_A_CHANGE_REQUEST,
                    "reason": "r",
                    "missing": [],
                    "clarification": "The staging subnet is 10.9.9.0/24.",
                }
            )
        ]
    )
    assert classify("what is the staging subnet?", provider).clarification == ""


def test_a_deflected_question_never_shows_gate_prose():
    advice = Advisor(
        provider=StubProvider(completions=[]),
        gate_provider=StubProvider(
            completions=[
                json.dumps(
                    {
                        "verdict": GateVerdict.NOT_A_CHANGE_REQUEST,
                        "reason": "r",
                        "missing": [],
                        "clarification": "Sure! The DC is alpha-7.",
                    }
                )
            ]
        ),
        range_md="# Range\n\nalpha-7\n",
        corpus_text="",
    ).advise("what is the domain controller called?")
    assert advice.kind == "deflection"
    assert "alpha-7" not in advice.public_text


def test_the_gate_prompt_asks_for_a_question_specific_example():
    provider = StubProvider(completions=[reply_with("ok")])
    classify("q", provider)
    system = provider.calls[0]["system"]
    assert "clarification" in system
    assert "example" in system.lower()


def test_the_schema_offers_the_clarification_field():
    provider = StubProvider(completions=[reply_with("ok")])
    classify("q", provider)
    assert "clarification" in provider.calls[0]["schema"]["properties"]
