"""Stage one: decide whether a message is a change request.

This runs with no range context whatsoever. A reconnaissance question is
terminated here, so the model holding the scenario never sees it.
"""

from __future__ import annotations

import json
import re

from rangecontrol.advisor.models import GateResult, GateVerdict, MissingDetail
from rangecontrol.advisor.prompts import GATE_SCHEMA, GATE_SYSTEM_PROMPT
from rangecontrol.llm.base import LLMError, Provider

_VALID = {
    GateVerdict.CHANGE_REQUEST,
    GateVerdict.NOT_A_CHANGE_REQUEST,
    GateVerdict.AMBIGUOUS,
}

# The default model (claude-opus-5) thinks by default: omitting `thinking`
# runs adaptive thinking at the default (high) effort, and `max_tokens` is a
# hard cap on thinking *plus* the response text together, not just the reply.
# A tight budget here doesn't produce a short answer — it produces a
# truncation that LLMError turns into a refusal for every single question.
# 4000 leaves room for a few hundred tokens of reasoning ahead of the small
# JSON verdict this stage returns. LLM_GATE_MODEL is the intended cost lever,
# not this constant.
GATE_MAX_TOKENS = 4000

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object, tolerating a surrounding markdown code fence.

    Anchored to the string's start and end, so a backtick inside a JSON string
    value is left alone. Handles a fence with or without a language tag and
    with or without an internal newline — a single-line fenced payload must
    not be reduced to the empty string, since in the ruling path a spurious
    parse failure becomes a refusal shown to the blue team.
    """
    text = _FENCE.sub("", raw.strip()).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned unparseable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def classify(question: str, provider: Provider) -> GateResult:
    """Classify a message's intent. Raises LLMError on unusable output."""
    if not question.strip():
        raise LLMError("empty question")

    raw = provider.complete(
        system=GATE_SYSTEM_PROMPT,
        user=f"Message:\n{question.strip()}",
        schema=GATE_SCHEMA,
        max_tokens=GATE_MAX_TOKENS,
    )
    payload = parse_json_object(raw)

    verdict = payload.get("verdict")
    if verdict not in _VALID:
        raise LLMError(f"gate returned an unknown verdict: {verdict!r}")

    return GateResult(
        verdict=verdict,
        reason=str(payload.get("reason", "")),
        missing=_missing(payload.get("missing")),
        clarification=_clarification(payload.get("clarification"), verdict),
    )


# A clarification is two or three sentences. Well past that means the model has
# left the task, and its output goes straight to the team unedited.
MAX_CLARIFICATION_CHARS = 800


def _clarification(raw, verdict: str) -> str:
    """Validate the gate's written ask, or return "" to use the canned one.

    Only the AMBIGUOUS path may speak model-written prose. A deflection is the
    reply reconnaissance questions receive, and it stays a fixed string: it is
    the one place improvisation must not be possible.

    This text is safe to show because of where it comes from, not because of
    what it says. The gate is never given the range document, so it has nothing
    to disclose. Everything checked here is shape, not content.
    """
    if verdict != GateVerdict.AMBIGUOUS or not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or len(text) > MAX_CLARIFICATION_CHARS:
        return ""
    return text


def _missing(raw) -> tuple[str, ...]:
    """Keep the known detail categories, in a stable order.

    Unknown or malformed entries are dropped rather than raising: there is no
    canned text to render them with, and a cosmetic slip in an advisory field
    must not become a refusal on a question the gate otherwise classified fine.
    """
    if not isinstance(raw, list):
        return ()
    seen = {item for item in raw if isinstance(item, str)}
    return tuple(name for name in MissingDetail.ALL if name in seen)
