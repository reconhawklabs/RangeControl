"""Public reply formatting.

The model already writes the exact words the blue team should see, so this
module adds no framing of its own — it only enforces Discord's length ceiling.
Any label, prefix, or verdict badge added here would risk correlating denials.
"""

from __future__ import annotations

from rangecontrol.advisor.models import Advice

DISCORD_LIMIT = 2000
_ELLIPSIS = "…"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def format_public(advice: Advice) -> str:
    """Return the message body to post in the requester's channel."""
    return _truncate(advice.public_text.strip(), DISCORD_LIMIT)
