import json

from rangecontrol.advisor.gate import classify
from rangecontrol.advisor.prompts import (
    GATE_SCHEMA,
    GATE_SYSTEM_PROMPT,
    REPLY_SCREEN_SCHEMA,
    REPLY_SCREEN_SYSTEM_PROMPT,
    RULING_SCHEMA,
    RULING_SYSTEM_PROMPT,
    STANDING_RULES_REMINDER,
)
from rangecontrol.advisor.ruling import adjudicate
from rangecontrol.llm.base import EFFORT_HIGH, EFFORT_MEDIUM
from tests.support.stub_provider import StubProvider


def test_schemas_are_strict_objects():
    """Strict JSON-schema mode rejects anything looser, and a rejected schema
    turns every question into the error text."""
    loose = [
        name
        for name, schema in (
            ("gate", GATE_SCHEMA),
            ("ruling", RULING_SCHEMA),
            ("reply screen", REPLY_SCREEN_SCHEMA),
        )
        if schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or set(schema.get("required", ())) != set(schema.get("properties", {}))
    ]
    assert not loose, loose


def test_ruling_prompt_states_the_disclosure_rules():
    """Echo-only identifiers, no inject-correlated denials, refusals in character."""
    lowered = RULING_SYSTEM_PROMPT.lower()
    assert "echo" in lowered or "already used" in lowered
    assert "never introduce" in lowered
    assert "inject" in lowered
    assert "owner of the network" in lowered


def test_ruling_prompt_never_invites_comment_on_what_exists():
    """The 2026-09-18 incident. The prompt used to say that telling the asker
    something "has no relationship to this environment" was useful and
    "reveals nothing". It reveals the asset inventory, which the blue team is
    supposed to build for itself, and the model did exactly as told."""
    lowered = RULING_SYSTEM_PROMPT.lower()
    assert "no relationship to this environment" not in lowered
    assert "reveals nothing" not in lowered
    # Existence is decided internally; it may only be written down there.
    assert "never confirm or deny" in lowered
    assert "internal_reason" in lowered.split("never confirm or deny")[0]


def test_existence_rule_is_restated_wherever_the_guarantee_is():
    """After operator guidance, and in the reviewer that backs the ruling up."""
    missing = [
        name
        for name, prompt in (
            ("STANDING_RULES_REMINDER", STANDING_RULES_REMINDER),
            ("REPLY_SCREEN_SYSTEM_PROMPT", REPLY_SCREEN_SYSTEM_PROMPT),
        )
        if "exist" not in prompt.lower()
    ]
    assert not missing, missing


def test_context_free_prompts_claim_no_knowledge_of_the_network():
    """The gate and the reviewer are safe because they know nothing."""
    assert "you do not have" in GATE_SYSTEM_PROMPT.lower()
    lowered = REPLY_SCREEN_SYSTEM_PROMPT.lower()
    assert "no knowledge of the network" in lowered
    assert "reference document" not in lowered


def test_each_stage_declares_its_effort():
    """The gate is a classification; the ruling is the decision that can
    break an exercise. Their reasoning budgets must not be left to whatever
    the vendor defaults to that month."""
    gate = StubProvider([json.dumps({"verdict": "CHANGE_REQUEST", "reason": "r"})])
    classify("block 203.0.113.10 at the edge", gate)
    assert gate.calls[0]["effort"] == EFFORT_MEDIUM

    ruler = StubProvider([json.dumps({
        "verdict": "APPROVED", "public_response": "ok", "internal_reason": "fine",
        "impacted": [], "confidence": "high",
    })])
    adjudicate("block 203.0.113.10", "context", ruler)
    assert ruler.calls[0]["effort"] == EFFORT_HIGH
