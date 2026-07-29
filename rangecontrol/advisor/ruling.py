"""Stage two: rule on a change request against the full range context."""

from __future__ import annotations

from rangecontrol.advisor.gate import parse_json_object
from rangecontrol.advisor.models import Ruling
from rangecontrol.advisor.prompts import RULING_SCHEMA
from rangecontrol.llm.base import LLMError, Provider

# Same reasoning as GATE_MAX_TOKENS: on a thinking-by-default model, this
# budget covers adaptive reasoning over the full range context as well as the
# JSON ruling itself. A tight cap here reads as a truncation failure — every
# ruling would come back as ERROR_TEXT — not as a short answer. 16000 leaves
# ample headroom for both.
RULING_MAX_TOKENS = 16000

_VERDICTS = {"APPROVED", "DENIED"}
_CONFIDENCE = {"high", "medium", "low"}


def adjudicate(question: str, context: str, provider: Provider) -> Ruling:
    """Rule on a change request. Raises LLMError on unusable output."""
    raw = provider.complete(
        system=context,
        user=f"Proposed change:\n{question.strip()}",
        schema=RULING_SCHEMA,
        max_tokens=RULING_MAX_TOKENS,
    )
    payload = parse_json_object(raw)

    verdict = payload.get("verdict")
    if verdict not in _VERDICTS:
        raise LLMError(f"ruling returned an unknown verdict: {verdict!r}")

    confidence = payload.get("confidence")
    if confidence not in _CONFIDENCE:
        raise LLMError(f"ruling returned an unknown confidence: {confidence!r}")

    # Type-check rather than coerce. str(None) is "None", which is non-empty and
    # would sail past an emptiness check straight to the blue team as a ruling.
    public_response = payload.get("public_response")
    if not isinstance(public_response, str):
        raise LLMError(
            f"public_response must be a string, got {type(public_response).__name__}"
        )
    if not public_response.strip():
        raise LLMError("ruling returned an empty public_response")

    internal_reason = payload.get("internal_reason")
    if not isinstance(internal_reason, str):
        raise LLMError(
            f"internal_reason must be a string, got {type(internal_reason).__name__}"
        )

    impacted = payload.get("impacted", [])
    if not isinstance(impacted, list):
        raise LLMError(f"impacted must be a list, got {type(impacted).__name__}")
    if not all(isinstance(item, str) for item in impacted):
        raise LLMError("impacted must contain only strings")

    return Ruling(
        verdict=verdict,
        public_response=public_response.strip(),
        internal_reason=internal_reason.strip(),
        impacted=tuple(impacted),
        confidence=confidence,
    )
