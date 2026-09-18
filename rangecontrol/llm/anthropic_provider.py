"""Anthropic adapter. The only file that imports the anthropic SDK."""

from __future__ import annotations

import base64
import logging

from rangecontrol.llm.base import LLMError, brief_error

logger = logging.getLogger(__name__)

# On a thinking-by-default model, max_tokens caps thinking plus response text
# together. A large request (e.g. Range.md generation at 32000) risks the
# SDK's non-streaming HTTP timeout, so anything at or above this threshold
# uses the streaming API and accumulates the final message instead — same
# response shape, no client-side timeout risk.
STREAM_THRESHOLD = 8000

# A diagram transcription can run to a couple of thousand tokens on its own,
# and on a thinking-by-default model the cap covers reasoning as well. 4000
# was observed truncating on a real range PDF before any text came back.
IMAGE_MAX_TOKENS = 16000

_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

# Range material is full of attack tooling, credential dumps, and planned
# intrusions: exactly what the safety classifiers on the newest models are
# tuned to decline, even though the request is a defensive ruling for the
# exercise's own control staff. Observed live: a Sonnet 5 ruling over a real
# range corpus came back stop_reason=refusal and the blue team got the
# generic "can't process that". The server-side fallback re-runs a declined
# request on the model Anthropic recommends for that refusal category, in
# the same round trip, so a classifier false positive costs latency rather
# than a ruling.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS = "default"


class AnthropicProvider:
    """Provider implementation backed by the Anthropic Messages API."""

    def __init__(self, api_key: str, model: str, client=None) -> None:
        self._model = model
        # Older models (Haiku 4.5, Sonnet 4.5) reject output_config.effort
        # with a 400. Learned once per provider on the first rejection and
        # never sent again, so a cheaper gate model keeps working.
        self._effort_unsupported = False
        # Same shape for the fallback parameter: a model or platform that
        # rejects it is retried once without and not asked again.
        self._fallbacks_unsupported = False
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
        effort: str | None = None,
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
        output_config: dict = {}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        return self._send(self._with_effort(kwargs, output_config, effort))

    def describe_image(
        self,
        *,
        data: bytes,
        mime_type: str,
        prompt: str,
        effort: str | None = None,
    ) -> str:
        encoded = base64.standard_b64encode(data).decode("ascii")
        kwargs = {
                "model": self._model,
                "max_tokens": IMAGE_MAX_TOKENS,
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
        return self._send(self._with_effort(kwargs, {}, effort))

    def _with_effort(self, kwargs: dict, output_config: dict, effort: str | None) -> dict:
        """Attach ``output_config`` (format and/or effort) to a request."""
        config = dict(output_config)
        if effort in _EFFORT_LEVELS and not self._effort_unsupported:
            config["effort"] = effort
        if config:
            return {**kwargs, "output_config": config}
        return kwargs

    def _send(self, kwargs: dict) -> str:
        try:
            response = self._request(kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately wrap every SDK failure
            if _rejects_effort(exc, kwargs):
                # Retry once without it and remember: a model that rejects
                # the parameter will keep rejecting it.
                self._effort_unsupported = True
                return self._send(_without_effort(kwargs))
            if not self._fallbacks_unsupported and _rejects_fallbacks(exc):
                self._fallbacks_unsupported = True
                return self._send(kwargs)
            raise LLMError(f"Anthropic request failed: {brief_error(exc)}") from exc
        _log_fallback(response, kwargs.get("model"))

        # Response parsing sits inside the boundary too: base.Provider promises
        # LLMError on response-shape failure, so a malformed or unexpected
        # response must never escape as a raw AttributeError/TypeError.
        try:
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "refusal":
                raise LLMError(_describe_refusal(response))
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

    def _request(self, kwargs: dict):
        messages = self._client.messages
        beta = getattr(self._client, "beta", None)
        if not self._fallbacks_unsupported and beta is not None:
            # Fallbacks ride on the beta namespace; everything else in the
            # request is identical, so the two paths share one kwargs dict.
            messages = beta.messages
            kwargs = {**kwargs, "betas": [FALLBACK_BETA], "fallbacks": FALLBACKS}
        if kwargs.get("max_tokens", 0) >= STREAM_THRESHOLD:
            with messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        return messages.create(**kwargs)


def _describe_refusal(response) -> str:
    """Name the refusal category so the white cell sees why, not just that."""
    details = getattr(response, "stop_details", None)
    category = getattr(details, "category", None) if details is not None else None
    explanation = getattr(details, "explanation", None) if details is not None else None
    text = "Anthropic declined the request (stop_reason=refusal"
    if category:
        text += f", category={category}"
    text += ")."
    if explanation:
        text += f" {str(explanation).strip()}"
    return text


def _log_fallback(response, requested: str | None) -> None:
    served = getattr(response, "model", None)
    if served and requested and served != requested:
        logger.warning(
            "%s declined this request; the server-side fallback %s answered it",
            requested,
            served,
        )


def _rejects_fallbacks(exc: Exception) -> bool:
    if getattr(exc, "status_code", 400) != 400:
        return False
    message = brief_error(exc).lower()
    return "fallback" in message or "server-side-fallback" in message


def _rejects_effort(exc: Exception, kwargs: dict) -> bool:
    if "effort" not in kwargs.get("output_config", {}):
        return False
    if getattr(exc, "status_code", 400) != 400:
        return False
    return "effort" in brief_error(exc).lower()


def _without_effort(kwargs: dict) -> dict:
    config = {k: v for k, v in kwargs.get("output_config", {}).items() if k != "effort"}
    trimmed = {k: v for k, v in kwargs.items() if k != "output_config"}
    if config:
        trimmed["output_config"] = config
    return trimmed
