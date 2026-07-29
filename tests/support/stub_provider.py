"""Deterministic offline Provider for tests. Never touches the network."""

from __future__ import annotations


class StubProvider:
    """Returns queued completions in order and records every call."""

    def __init__(
        self,
        completions: list[str] | None = None,
        image_text: str = "stub image description",
        error: Exception | None = None,
    ) -> None:
        self._completions = list(completions or [])
        self._image_text = image_text
        self._error = error
        self.calls: list[dict] = []
        self.image_calls: list[dict] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        max_tokens: int = 2000,
    ) -> str:
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "max_tokens": max_tokens}
        )
        if self._error is not None:
            raise self._error
        assert self._completions, "StubProvider ran out of queued completions"
        return self._completions.pop(0)

    def describe_image(self, *, data: bytes, mime_type: str, prompt: str) -> str:
        self.image_calls.append(
            {"size": len(data), "mime_type": mime_type, "prompt": prompt}
        )
        if self._error is not None:
            raise self._error
        return self._image_text
