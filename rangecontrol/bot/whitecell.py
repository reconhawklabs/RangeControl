"""White cell audit embed.

embed_fields is pure so it can be tested without a Discord connection;
build_embed is the thin discord.py wrapper around it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rangecontrol.advisor.models import Advice

if TYPE_CHECKING:  # pragma: no cover - import only for the return annotation
    import discord

FIELD_LIMIT = 1024
NAME_LIMIT = 256

_COLOURS = {
    "APPROVED": 0x2ECC71,
    "DENIED": 0xE74C3C,
}
_ERROR_COLOUR = 0x992D22
_NEUTRAL_COLOUR = 0x95A5A6

AWAITING_FIELD = (
    "Awaiting review",
    "React with ✅ to release this reply to the blue team, "
    "or ❌ to deny it.",
    False,
)


def _clip(text: str, limit: int = FIELD_LIMIT) -> str:
    text = text.strip() or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


_NOT_HELD_TEXT = (
    "Human in the loop is on, but only rulings are held for approval. This "
    "was a {kind}, which carries no adjudication to review, so it went to "
    "the blue team immediately."
)


def embed_fields(
    advice: Advice,
    *,
    user_name: str,
    channel_name: str,
    question: str,
    awaiting: bool = False,
    hitl_enabled: bool = False,
) -> list[tuple[str, str, bool]]:
    """Return (name, value, inline) triples for the audit embed.

    ``awaiting`` appends a field instructing the white cell to react when this
    ruling is being held for human-in-the-loop approval.
    """
    fields: list[tuple[str, str, bool]] = [
        ("Asker", _clip(user_name, NAME_LIMIT), True),
        ("Channel", _clip(channel_name, NAME_LIMIT), True),
        ("Question", _clip(question), False),
    ]

    ruling = advice.ruling
    if ruling is not None:
        confidence = ruling.confidence
        if confidence == "low":
            confidence = "⚠️ LOW — review this ruling"
        fields.extend(
            [
                ("Verdict", ruling.verdict, True),
                ("Confidence", confidence, True),
                ("Real reason", _clip(ruling.internal_reason), False),
                (
                    "Impacted",
                    _clip(", ".join(ruling.impacted) if ruling.impacted else "none"),
                    False,
                ),
            ]
        )
    else:
        fields.append(("Outcome", advice.kind, True))

    fields.append(("Sent to blue team", _clip(advice.public_text), False))

    if advice.error:
        fields.append(("Error", _clip(advice.error), False))

    # Say why nothing is waiting on the reviewer. Without this, the embed for
    # a deflection is indistinguishable from a ruling whose approval prompt
    # failed to appear, and the operator reasonably reads the mode as broken.
    if hitl_enabled and not awaiting:
        fields.append(
            ("Not held", _clip(_NOT_HELD_TEXT.format(kind=advice.kind)), False)
        )

    if awaiting:
        fields.append(AWAITING_FIELD)

    return fields


def _colour(advice: Advice) -> int:
    if advice.kind == "error":
        return _ERROR_COLOUR
    if advice.ruling is not None:
        return _COLOURS.get(advice.ruling.verdict, _NEUTRAL_COLOUR)
    return _NEUTRAL_COLOUR


def build_embed(
    advice: Advice,
    *,
    user_name: str,
    channel_name: str,
    question: str,
    awaiting: bool = False,
    hitl_enabled: bool = False,
) -> "discord.Embed":
    import discord

    title = f"RangeControl — {advice.kind}"
    if awaiting:
        title = f"⏳ AWAITING REVIEW — {title}"

    embed = discord.Embed(title=title, colour=_colour(advice))
    for name, value, inline in embed_fields(
        advice,
        user_name=user_name,
        channel_name=channel_name,
        question=question,
        awaiting=awaiting,
        hitl_enabled=hitl_enabled,
    ):
        embed.add_field(name=name, value=value, inline=inline)
    return embed
