"""Google Gemini adapter. The only file that imports the google-genai SDK."""

from __future__ import annotations

import hashlib
import logging
import threading

from rangecontrol.llm.base import LLMError, brief_error

logger = logging.getLogger(__name__)

# The standing context (Range.md plus the whole corpus) is identical on every
# question, so it is worth uploading once and referencing rather than re-sending.
# Anthropic gets this via cache_control; Gemini needs an explicit cached-content
# handle. Below the threshold the round trip to create one costs more than it
# saves, and Gemini rejects content under its own minimum anyway.
_CACHE_MIN_CHARS = 32_768
_CACHE_TTL = "3600s"

# Gemini's response_schema is an OpenAPI 3.0 subset and rejects unknown keys
# outright (400 INVALID_ARGUMENT). `additionalProperties` is mandatory for
# Anthropic's strict JSON-schema mode, so the shared schema constants carry it
# and it has to come off before the request reaches Gemini.
#
# Note the SDK will not catch this for you: it accepts the key client-side via
# a Pydantic alias and forwards it, so the failure only appears against the
# live API.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"additionalProperties", "additional_properties", "$schema"}
)


# Range material describes planned intrusions and carries dumped credentials;
# Gemini's default safety thresholds block responses about it outright. Only
# the highest-probability harm is blocked, matching the sibling Anthropic
# adapter's use of server-side refusal fallbacks.
_SAFETY_SETTINGS = [
    {"category": category, "threshold": "BLOCK_ONLY_HIGH"}
    for category in (
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    )
]


class _RequestFailed(LLMError):
    """The call itself failed, before the model produced anything.

    Distinguished from the other LLMErrors this module raises because only a
    request-layer failure can be caused by a stale cached-content handle. A bad
    finish_reason or an unusable response shape means the request succeeded, so
    retrying those without the cache would just pay for the same failure twice.
    """


def _to_gemini_schema(schema: dict) -> dict:
    """Return a deep copy of ``schema`` with keys Gemini rejects removed.

    Copies rather than mutating: the schema constants are shared with the
    Anthropic adapter, which requires the keys stripped here.
    """
    cleaned: dict = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if isinstance(value, dict):
            cleaned[key] = _to_gemini_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _to_gemini_schema(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


class GeminiProvider:
    """Provider implementation backed by the Google Gemini API."""

    def __init__(self, api_key: str, model: str, client=None) -> None:
        self._model = model
        self._cache_name: str | None = None
        self._cache_key: str | None = None
        self._caching_unavailable = False
        # Rulings run on worker threads, so two questions can reach this
        # provider at once. The handle is created under a lock so they
        # share one upload of the standing context instead of racing into
        # two.
        self._cache_lock = threading.Lock()
        if client is not None:
            self._client = client
        else:
            from google import genai

            self._client = genai.Client(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        max_tokens: int = 2000,
        effort: str | None = None,
    ) -> str:
        # ``effort`` is accepted for protocol parity and left to Gemini's own
        # dynamic thinking: its budget parameter is model-specific and a
        # value a model rejects would cost every ruling.
        config: dict = {
            "max_output_tokens": max_tokens,
            "safety_settings": list(_SAFETY_SETTINGS),
        }
        if schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = _to_gemini_schema(schema)

        cached = self._cached_handle(system)
        if cached is None:
            config["system_instruction"] = system
            return self._send(contents=user, config=config)

        # cached_content carries the system instruction; the two are exclusive.
        config["cached_content"] = cached
        try:
            return self._send(contents=user, config=config)
        except _RequestFailed:
            # A cache can expire or be deleted mid-exercise. That must never cost
            # a ruling, so drop the handle and answer inline this once.
            logger.warning("Gemini cached context unusable; retrying inline")
            with self._cache_lock:
                if self._cache_name == cached:
                    self._cache_name = None
                    self._cache_key = None
            # A fresh dict, not a mutation: the first config was already handed
            # to the SDK, and editing it in place would rewrite that call's
            # arguments underneath it.
            retry = {k: v for k, v in config.items() if k != "cached_content"}
            retry["system_instruction"] = system
            return self._send(contents=user, config=retry)

    def _cached_handle(self, system: str) -> str | None:
        """Return a cached-content name for ``system``, or None to send inline.

        Caching is an optimisation. Every failure path here falls back to an
        inline system instruction rather than raising — a caching problem must
        never stop the bot answering.
        """
        if self._caching_unavailable or len(system) < _CACHE_MIN_CHARS:
            return None

        key = hashlib.sha256(system.encode("utf-8")).hexdigest()
        with self._cache_lock:
            return self._cached_handle_locked(system, key)

    def _cached_handle_locked(self, system: str, key: str) -> str | None:
        if self._caching_unavailable:
            return None
        if self._cache_name is not None and self._cache_key == key:
            return self._cache_name

        try:
            created = self._client.caches.create(
                model=self._model,
                config={
                    "system_instruction": system,
                    "ttl": _CACHE_TTL,
                    "display_name": "rangecontrol-standing-context",
                },
            )
            name = created.name
        except Exception:  # noqa: BLE001 - optimisation only, never fatal
            logger.warning(
                "Gemini context caching unavailable; the standing context will "
                "be re-sent with every question",
                exc_info=True,
            )
            self._caching_unavailable = True
            return None

        self._cache_name = name
        self._cache_key = key
        logger.info("Gemini standing context cached (%s)", name)
        return name

    def describe_image(
        self,
        *,
        data: bytes,
        mime_type: str,
        prompt: str,
        effort: str | None = None,
    ) -> str:
        return self._send(
            contents=[{"inline_data": {"mime_type": mime_type, "data": data}}, prompt],
            # Matches the Anthropic sibling's describe_image budget: on a
            # thinking-by-default model the cap covers reasoning as well as
            # the description, so a tight number reads as truncation rather
            # than a short answer.
            config={
                "max_output_tokens": 16000,
                "safety_settings": list(_SAFETY_SETTINGS),
            },
        )

    def _send(self, *, contents, config: dict) -> str:
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except Exception as exc:  # noqa: BLE001 - deliberately wrap every SDK failure
            raise _RequestFailed(
                f"Gemini request failed: {brief_error(exc)}"
            ) from exc

        # Response parsing sits inside the boundary too: base.Provider promises
        # LLMError on response-shape failure. `.text` is an SDK *property*, so
        # getattr's default only absorbs a missing attribute — an exception
        # raised inside the property still escapes. Wrap it.
        try:
            # Truncation is a failure, not a short answer. The sibling Anthropic
            # adapter raises on stop_reason == "max_tokens"; without the same
            # check here a truncated document or ruling would be returned as if
            # complete, and nothing downstream can tell the difference.
            #
            # The same applies to every other abnormal finish reason: Gemini's
            # SAFETY / RECITATION / PROHIBITED_CONTENT / BLOCKLIST / SPII can
            # all stop a response after partial content, and the Anthropic
            # adapter already treats its own refusal signal (stop_reason ==
            # "refusal") as an error. Anything that isn't STOP (or absent) is
            # therefore also an error here, not a shorter-than-usual answer —
            # otherwise a partial-then-blocked description is returned as if
            # complete and, for describe_image, gets written to the
            # content-addressed cache and replayed on every later run.
            for candidate in getattr(response, "candidates", None) or ():
                reason = getattr(candidate, "finish_reason", None)
                if reason is None:
                    continue
                name = getattr(reason, "name", None) or str(reason)
                if name == "STOP":
                    continue
                if name.endswith("MAX_TOKENS"):
                    raise LLMError(
                        "Gemini response was truncated (finish_reason=MAX_TOKENS)."
                    )
                raise LLMError(
                    f"Gemini declined or stopped abnormally (finish_reason={name})."
                )
            text = getattr(response, "text", None)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed response shape
            raise LLMError(
                f"Gemini returned an unusable response shape: {type(exc).__name__}"
            ) from exc

        if not text or not text.strip():
            raise LLMError("Gemini returned no text content.")
        return text
