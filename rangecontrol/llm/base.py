"""Provider-agnostic LLM interface.

No provider-specific import may appear outside this package.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """Any failure reaching or parsing a response from an LLM provider."""


class UsageLimitError(LLMError):
    """The provider refused because the account hit a usage, spend, rate, or
    quota limit. Retrying now will not help; access returns on its own or when
    the limit is raised."""


# How the vendors word a limit: Anthropic's workspace spend limit ("You have
# reached your specified API usage limits", a 400), its billing refusal
# ("credit balance is too low"), rate limits, and Gemini's RESOURCE_EXHAUSTED.
_LIMIT_MESSAGE = re.compile(
    r"usage limit|spend(?:ing)? limit|credit balance|rate.?limit|quota|"
    r"resource.?exhausted",
    re.IGNORECASE,
)


def is_usage_limit(exc: BaseException) -> bool:
    """Whether an SDK failure means the account is at a usage limit.

    HTTP 429 always is. Anthropic reports a spend limit as a 400, so the
    message decides there.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    message = str(getattr(exc, "message", None) or exc or "")
    return bool(_LIMIT_MESSAGE.search(message))


_BRIEF_LIMIT = 300


def brief_error(exc: BaseException) -> str:
    """``TypeName: message`` for an SDK failure, kept short.

    The type alone ("BadRequestError") tells an operator nothing; the
    message ("prompt is too long: 250000 tokens > 200000 maximum") is what
    they act on. API error messages describe the request, never echo it, so
    nothing from the range leaks through this. Capped so a wrapped HTML
    error page cannot flood a status bar.
    """
    message = str(getattr(exc, "message", None) or exc or "").strip()
    message = " ".join(message.split())
    if len(message) > _BRIEF_LIMIT:
        message = message[: _BRIEF_LIMIT - 1] + "…"
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


# Reasoning depth per call. Vendors that support an explicit level map these
# names onto their own parameter; others ignore them. On thinking-by-default
# models ``max_tokens`` caps reasoning and answer together, so the level and
# the budget are chosen as a pair by each caller.
EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"


@runtime_checkable
class Provider(Protocol):
    """The two capabilities RangeControl needs from a model vendor."""

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        max_tokens: int = 2000,
        effort: str | None = None,
    ) -> str:
        """Return the model's text response.

        When ``schema`` is given the response is constrained to that JSON
        schema and the returned string is raw JSON. ``effort`` is one of the
        EFFORT_* names or None for the vendor default.

        Raises:
            LLMError: on any transport, auth, or response-shape failure.
        """
        ...

    def describe_image(
        self,
        *,
        data: bytes,
        mime_type: str,
        prompt: str,
        effort: str | None = None,
    ) -> str:
        """Return a textual description of an image.

        Raises:
            LLMError: on any transport, auth, or response-shape failure.
        """
        ...
