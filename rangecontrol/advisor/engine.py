"""Gate → ruling orchestration.

Advisor.advise never raises. Every failure path returns an error Advice whose
public text is a refusal to process, so a broken API key or a malformed
response can never become an accidental approval.
"""

from __future__ import annotations

import logging

from rangecontrol.advisor.gate import classify
from rangecontrol.advisor.models import Advice, GateVerdict
from rangecontrol.advisor.prompts import (
    CLARIFY_TEXT,
    DEFLECTION_TEXT,
    ERROR_TEXT,
    build_ruling_context,
    clarify_text,
)
from rangecontrol.advisor.ruling import adjudicate
from rangecontrol.llm.base import Provider

logger = logging.getLogger(__name__)


class Advisor:
    def __init__(
        self,
        provider: Provider,
        gate_provider: Provider,
        range_md: str,
        corpus_text: str,
        extra_instructions: str = "",
    ) -> None:
        self._provider = provider
        self._gate_provider = gate_provider
        # Built once so the cached prefix stays byte-identical across questions.
        self._context = build_ruling_context(
            range_md, corpus_text, extra_instructions
        )

    def advise(self, question: str) -> Advice:
        """Adjudicate one question. Never raises."""
        if not question.strip():
            return Advice(kind="clarify", public_text=CLARIFY_TEXT)

        try:
            gate = classify(question, self._gate_provider)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            logger.exception("intent gate failed")
            return Advice(kind="error", public_text=ERROR_TEXT, error=repr(exc))

        if gate.verdict == GateVerdict.NOT_A_CHANGE_REQUEST:
            return Advice(kind="deflection", public_text=DEFLECTION_TEXT, gate=gate)
        if gate.verdict == GateVerdict.AMBIGUOUS:
            # The gate's own wording when it produced usable text, since a
            # fixed checklist answers a password question by asking about
            # firewalls. Safe because the gate is never given the range
            # document: it can only work from the sender's own words. The
            # canned per-category ask is the fallback.
            return Advice(
                kind="clarify",
                public_text=gate.clarification or clarify_text(gate.missing),
                gate=gate,
            )

        try:
            ruling = adjudicate(question, self._context, self._provider)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            logger.exception("ruling failed")
            return Advice(
                kind="error", public_text=ERROR_TEXT, gate=gate, error=repr(exc)
            )

        return Advice(
            kind="ruling",
            public_text=ruling.public_response,
            ruling=ruling,
            gate=gate,
        )
