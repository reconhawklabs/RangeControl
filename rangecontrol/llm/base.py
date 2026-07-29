"""Provider-agnostic LLM interface.

No provider-specific import may appear outside this package.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """Any failure reaching or parsing a response from an LLM provider."""


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
    ) -> str:
        """Return the model's text response.

        When ``schema`` is given the response is constrained to that JSON
        schema and the returned string is raw JSON.

        Raises:
            LLMError: on any transport, auth, or response-shape failure.
        """
        ...

    def describe_image(self, *, data: bytes, mime_type: str, prompt: str) -> str:
        """Return a textual description of an image.

        Raises:
            LLMError: on any transport, auth, or response-shape failure.
        """
        ...
