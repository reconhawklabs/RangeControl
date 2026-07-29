import pytest

from rangecontrol.advisor.models import Advice, GateResult, GateVerdict, Ruling
from rangecontrol.advisor.prompts import (
    CLARIFY_TEXT,
    DEFLECTION_TEXT,
    ERROR_TEXT,
    GATE_SCHEMA,
    GATE_SYSTEM_PROMPT,
    RULING_SCHEMA,
    RULING_SYSTEM_PROMPT,
    build_ruling_context,
)


def schemas():
    return [GATE_SCHEMA, RULING_SCHEMA]


@pytest.mark.parametrize("schema", schemas())
def test_schemas_are_strict_objects(schema):
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_gate_schema_enumerates_the_three_verdicts():
    values = set(GATE_SCHEMA["properties"]["verdict"]["enum"])
    assert values == {
        GateVerdict.CHANGE_REQUEST,
        GateVerdict.NOT_A_CHANGE_REQUEST,
        GateVerdict.AMBIGUOUS,
    }


def test_ruling_schema_shape():
    props = RULING_SCHEMA["properties"]
    assert set(props["verdict"]["enum"]) == {"APPROVED", "DENIED"}
    assert set(props["confidence"]["enum"]) == {"high", "medium", "low"}
    assert props["impacted"]["type"] == "array"


def test_gate_prompt_receives_no_range_context():
    lowered = GATE_SYSTEM_PROMPT.lower()
    assert "you do not have" in lowered or "no knowledge" in lowered
    assert "change" in lowered


def test_ruling_prompt_states_the_echo_only_rule():
    lowered = RULING_SYSTEM_PROMPT.lower()
    assert "echo" in lowered or "already used" in lowered
    assert "never introduce" in lowered


def test_ruling_prompt_forbids_correlated_denials():
    lowered = RULING_SYSTEM_PROMPT.lower()
    assert "inject" in lowered
    assert "must not" in lowered or "never" in lowered


def test_ruling_prompt_requires_in_character_denials():
    lowered = RULING_SYSTEM_PROMPT.lower()
    assert "owner of the network" in lowered


def test_canned_replies_contain_no_range_specifics():
    for text in (DEFLECTION_TEXT, CLARIFY_TEXT, ERROR_TEXT):
        assert text.strip()
        assert "inject" not in text.lower()
        assert "msel" not in text.lower()


def test_context_contains_both_halves_and_labels_them():
    context = build_ruling_context("RANGE BODY", "CORPUS BODY")
    assert "RANGE BODY" in context
    assert "CORPUS BODY" in context
    assert context.index("RANGE BODY") < context.index("CORPUS BODY")


def test_context_is_stable_for_prompt_caching():
    assert build_ruling_context("a", "b") == build_ruling_context("a", "b")


def test_ruling_is_frozen():
    ruling = Ruling(
        verdict="APPROVED",
        public_response="ok",
        internal_reason="fine",
        impacted=(),
        confidence="high",
    )
    with pytest.raises(Exception):
        ruling.verdict = "DENIED"  # type: ignore[misc]


def test_advice_defaults():
    advice = Advice(kind="deflection", public_text="no")
    assert advice.ruling is None
    assert advice.gate is None
    assert advice.error is None


def test_gate_result_defaults():
    assert GateResult(verdict=GateVerdict.AMBIGUOUS).reason == ""
