"""Anthropic adapter. The only file that imports the anthropic SDK."""

from __future__ import annotations

import base64

from rangecontrol.llm.base import LLMError

# On a thinking-by-default model, max_tokens caps thinking plus response text
# together. A large request (e.g. Range.md generation at 32000) risks the
# SDK's non-streaming HTTP timeout, so anything at or above this threshold
# uses the streaming API and accumulates the final message instead — same
# response shape, no client-side timeout risk.
STREAM_THRESHOLD = 8000


class AnthropicProvider:
    """Provider implementation backed by the Anthropic Messages API."""

    def __init__(self, api_key: str, model: str, client=None) -> None:
        self._model = model
        if client is not None:
            self._client = client
        else:
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        max_tokens: int = 2000,
    ) -> str:
        kwargs = {
            "model": self._model,
            "max_tokens": max_tokens,
            # A list (not a bare string) so the block can carry cache_control.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user}],
        }
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return self._send(kwargs)

    def describe_image(self, *, data: bytes, mime_type: str, prompt: str) -> str:
        encoded = base64.standard_b64encode(data).decode("ascii")
        return self._send(
            {
                "model": self._model,
                # See STREAM_THRESHOLD's comment: on a thinking-by-default
                # model this budget covers reasoning as well as the
                # description, so it must not be sized as if it were
                # output-only.
                "max_tokens": 4000,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": encoded,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            }
        )

    def _send(self, kwargs: dict) -> str:
        try:
            if kwargs.get("max_tokens", 0) >= STREAM_THRESHOLD:
                with self._client.messages.stream(**kwargs) as stream:
                    response = stream.get_final_message()
            else:
                response = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately wrap every SDK failure
            raise LLMError(f"Anthropic request failed: {type(exc).__name__}") from exc

        # Response parsing sits inside the boundary too: base.Provider promises
        # LLMError on response-shape failure, so a malformed or unexpected
        # response must never escape as a raw AttributeError/TypeError.
        try:
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "refusal":
                raise LLMError("Anthropic declined the request (stop_reason=refusal).")
            if stop_reason == "max_tokens":
                raise LLMError("Anthropic response was truncated (stop_reason=max_tokens).")

            text = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed response shape
            raise LLMError(
                f"Anthropic returned an unusable response shape: {type(exc).__name__}"
            ) from exc

        if not text.strip():
            raise LLMError("Anthropic returned no text content.")
        return text
