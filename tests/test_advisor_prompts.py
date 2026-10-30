import json
import re

from rangecontrol.advisor.gate import classify
from rangecontrol.advisor.prompts import (
    DEFLECTION_TEXT,
    GATE_SCHEMA,
    GATE_SYSTEM_PROMPT,
    REPLY_SCREEN_SCHEMA,
    REPLY_SCREEN_SYSTEM_PROMPT,
    RULING_SCHEMA,
    RULING_SYSTEM_PROMPT,
    SCREENED_APPROVALS,
    SCREENED_DENIALS,
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


def test_the_ruling_reasons_before_it_decides():
    """2026-10-28: "Disable user account access for chief on 10.1.12.1" was
    DENIED while its own internal_reason concluded "nothing depends on it".
    Structured output is written in schema order, and the verdict came first:
    the model committed before it had reasoned. The reasoning and what it
    affects must come first, the verdict after, the reply the team reads
    last."""
    order = list(RULING_SCHEMA["properties"])
    assert order.index("internal_reason") < order.index("verdict"), order
    assert order.index("impacted") < order.index("verdict"), order
    assert order[-1] == "public_response", order
    assert RULING_SCHEMA["required"] == order
    # Uncertainty about an unknown name is not a dependency.
    lowered = " ".join(RULING_SYSTEM_PROMPT.lower().split())
    assert "name the specific dependency" in lowered


def test_replies_say_the_team_makes_the_change():
    """2026-10-28: teams thought the bot made changes itself ("I only handle
    change requests ... I'll get you an answer", "I can't action that"). The
    bot only approves or denies; the asker makes every change. Every text the
    bot writes without a model must say so or at least never promise to act,
    and both models that write replies are told."""
    from rangecontrol.bot.client import HELD_TEXT

    acts = re.compile(
        r"\bI(?:'ll| will)\s+(?:make|apply|carry|implement|disable|block|do|"
        r"action|change|push)\b|\bactioned\b",
        re.IGNORECASE,
    )
    canned = {
        "DEFLECTION_TEXT": DEFLECTION_TEXT,
        "HELD_TEXT": HELD_TEXT,
        **{f"fallback {i}": t for i, t in enumerate(SCREENED_APPROVALS + SCREENED_DENIALS)},
    }
    promising = {name: text for name, text in canned.items() if acts.search(text)}
    assert not promising, promising
    assert "don't make changes" in DEFLECTION_TEXT.lower()
    assert "before you make" in HELD_TEXT.lower()
    for name, prompt in (("ruling", RULING_SYSTEM_PROMPT), ("gate", GATE_SYSTEM_PROMPT)):
        assert "makes the change themselves" in " ".join(prompt.lower().split()), name


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


def test_ruling_prompt_protects_the_range_not_the_storyline():
    """The 2026-10-27 run: 24 of 25 rulings denied. Requests to disable
    accounts the range does not contain were refused because the source go
    book said account changes need an amended MOU and a change-request form.
    The asker cannot attach either, and the bot is the change board those
    rules point at. Range integrity decides; in-character process does not."""
    lowered = " ".join(RULING_SYSTEM_PROMPT.lower().split())
    assert "storyline" in lowered
    assert "memoranda of understanding" in lowered
    assert "your ruling is the sign-off" in lowered
    # An identifier nothing names cannot be something anything depends on.
    assert "nothing in it can depend on" in lowered
    # Scans were denied for "exploit-style probes" the document never claimed.
    assert "read-only" in lowered
    assert "risk the reference document does not state" in lowered
    # Designer denials still bind; only paperwork is set aside.
    assert "those entries bind as written" in lowered


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
