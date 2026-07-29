"""Frozen domain objects for the two-stage adjudication pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class GateVerdict:
    """Stage-one intent classification outcomes."""

    CHANGE_REQUEST: Final = "CHANGE_REQUEST"
    NOT_A_CHANGE_REQUEST: Final = "NOT_A_CHANGE_REQUEST"
    AMBIGUOUS: Final = "AMBIGUOUS"


class MissingDetail:
    """What an under-specified change request failed to supply.

    Deliberately generic. These are categories of detail any change request
    needs regardless of environment, so the clarification built from them says
    nothing about this one. The gate that selects them has no range context at
    all, which is what makes asking for detail safe.
    """

    TARGET: Final = "target"  # the address, host, account, or service acted on
    LOCATION: Final = "location"  # the device that would enforce the change
    ACTION: Final = "action"  # what would actually change
    SCOPE: Final = "scope"  # direction, duration, breadth

    ALL: Final = (TARGET, LOCATION, ACTION, SCOPE)


@dataclass(frozen=True)
class GateResult:
    verdict: str
    reason: str = ""
    missing: tuple[str, ...] = ()
    # Prose written by the gate for an AMBIGUOUS message, phrased in the
    # sender's own terms. Empty for every other verdict and whenever the text
    # failed validation, in which case the canned per-category ask is used.
    clarification: str = ""


@dataclass(frozen=True)
class Ruling:
    """One adjudication.

    The fields straddle the project's disclosure boundary. ``public_response``
    is written for the blue team. ``internal_reason`` and ``impacted`` name
    injects and assets explicitly and are for the white cell channel and the
    audit log only — routing either of them to a public reply is the single
    worst failure this system can have.
    """

    verdict: str  # "APPROVED" | "DENIED"
    public_response: str  # safe to post publicly
    internal_reason: str  # NEVER shown to the person asking
    impacted: tuple[str, ...]  # NEVER shown to the person asking
    confidence: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class Advice:
    """The complete outcome of one question.

    ``kind`` is one of "ruling", "deflection", "clarify", or "error".
    ``public_text`` is always safe to send to the blue team.
    """

    kind: str
    public_text: str
    ruling: Ruling | None = None
    gate: GateResult | None = None
    error: str | None = None
