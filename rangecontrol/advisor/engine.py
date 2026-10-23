"""Gate → ruling → reply screen orchestration.

Advisor.advise never raises. Every failure path returns an error Advice whose
public text is a refusal to process, so a broken API key or a malformed
response can never become an accidental approval.
"""

from __future__ import annotations

import logging
import random

from rangecontrol.advisor.gate import classify
from rangecontrol.advisor.models import Advice, GateVerdict, Ruling
from rangecontrol.advisor.prompts import (
    CLARIFY_TEXT,
    DEFLECTION_TEXT,
    ERROR_TEXT,
    build_ruling_context,
    clarify_text,
)
from rangecontrol.advisor.ruling import adjudicate
from rangecontrol.advisor.screen import (
    check_reply,
    fallback_reply,
    range_vocabulary,
    review_reply,
)
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
        # Operator guidance is included: it is where a white cell is most
        # likely to name a host it wants protected.
        self._vocabulary = range_vocabulary(
            range_md, corpus_text, extra_instructions
        )
        self._rng = random.SystemRandom()

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

        screen_reason = self._screen(question, ruling)
        if screen_reason:
            logger.warning("ruling reply withheld by the screen: %s", screen_reason)
        return Advice(
            kind="ruling",
            public_text=(
                fallback_reply(ruling.verdict, self._rng)
                if screen_reason
                else ruling.public_response
            ),
            ruling=ruling,
            gate=gate,
            screen_reason=screen_reason,
        )

    def _screen(self, question: str, ruling: Ruling) -> str:
        """Return why the ruling's own reply must be withheld, or "".

        The reviewer runs on the ruling provider, not the gate's: it is the
        last judgement between the strongest model's prose and the blue team,
        and LLM_GATE_MODEL is a cost lever that may point at a weaker model.
        It never sees the range context either way.

        It runs even when the deterministic check has already decided. A
        screened reply that skipped a model call would come back measurably
        faster than a clean one, and the delay would become the tell.

        A reviewer failure withholds the reply rather than erroring the whole
        ruling: the verdict is sound and the team is owed one; only the
        wording is unverified, and the fallback wording needs no verifying.
        """
        draft = ruling.public_response
        reasons = [check_reply(draft, question, self._vocabulary)]
        try:
            reasons.append(review_reply(draft, question, self._provider))
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            logger.exception("reply review failed")
            reasons.append(f"reply review failed ({exc!r}); withheld as a precaution")
        return "; ".join(reason for reason in reasons if reason)
