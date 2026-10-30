import threading
import time

import pytest

from rangecontrol.llm.base import LLMError, Provider, UsageLimitError
from rangecontrol.llm.gemini_provider import GeminiProvider


class FakeModels:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return type("R", (), {"text": self.text})()


class FakeClient:
    """No ``caches`` attribute: older SDKs and plain doubles look like this."""

    def __init__(self, models):
        self.models = models


def build(text=None, error=None):
    models = FakeModels(text=text, error=error)
    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=FakeClient(models))
    return provider, models


def returning(response):
    """A provider whose SDK hands back exactly ``response``."""

    class Models:
        def generate_content(self, **kwargs):
            return response

    return GeminiProvider(api_key="k", model="gemini-2.5-pro", client=FakeClient(Models()))


def finished(reason, text):
    candidate = type("Candidate", (), {"finish_reason": reason})()
    return type("R", (), {"candidates": [candidate], "text": text})()


class ExplodingText:
    @property
    def text(self):
        raise RuntimeError("malformed candidate list")


def test_every_unusable_response_fails_closed_with_llm_error():
    """Truncation and safety stops can arrive with partial text attached; that
    text must never be returned as if it were a complete answer."""
    assert returning(finished("STOP", "a complete answer")).complete(
        system="s", user="u"
    ) == "a complete answer"

    with pytest.raises(LLMError, match="truncated"):
        returning(finished("MAX_TOKENS", "a partial document that stops mid-")).complete(
            system="s", user="u"
        )
    with pytest.raises(LLMError, match="SAFETY"):
        returning(finished("SAFETY", "a partial answer cut off by the filt")).complete(
            system="s", user="u"
        )

    cases = {
        "no text attribute": returning(type("NoText", (), {"candidates": []})()),
        "raising text property": returning(ExplodingText()),
        "blank text": build("   ")[0],
        "none text": build(None)[0],
        "sdk exception": build(error=RuntimeError("network down"))[0],
    }
    escaped = []
    for label, provider in cases.items():
        try:
            provider.complete(system="s", user="u")
        except LLMError:
            continue
        escaped.append(label)
    assert escaped == []


def test_request_carries_system_relaxed_safety_and_a_gemini_safe_schema():
    """Gemini 400s on additionalProperties, which Anthropic's strict mode
    requires; the shared schema constants must keep it for the Anthropic side."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["APPROVED", "DENIED"]},
            "items": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": False},
            },
        },
        "required": ["verdict"],
    }
    provider, models = build("{}")
    assert isinstance(provider, Provider)
    provider.complete(system="sys", user="usr", schema=schema)

    config = models.kwargs["config"]
    assert config["system_instruction"] == "sys"
    assert models.kwargs["contents"] == "usr"
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"] == {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["APPROVED", "DENIED"]},
            "items": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["verdict"],
    }
    # The caller's schema is untouched at every depth.
    assert schema["additionalProperties"] is False
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False

    settings = config["safety_settings"]
    assert all(item["threshold"] == "BLOCK_ONLY_HIGH" for item in settings)
    assert any(item["category"] == "HARM_CATEGORY_DANGEROUS_CONTENT" for item in settings)


def test_describe_image_sends_an_inline_part():
    provider, models = build("a diagram")
    assert provider.describe_image(data=b"x", mime_type="image/png", prompt="p") == "a diagram"
    parts = models.kwargs["contents"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert parts[1] == "p"


# --- context caching -------------------------------------------------------
#
# The standing context is identical on every question, so it is uploaded once
# and referenced rather than re-sent. Caching is an optimisation: every failure
# path must fall back to an inline system instruction, never to a failed ruling.

BIG_SYSTEM = "x" * 40_000  # over _CACHE_MIN_CHARS


class FakeCaches:
    def __init__(self, error=None, name="cachedContents/abc"):
        self.error = error
        self.name = name
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return type("C", (), {"name": self.name})()


class CachingClient:
    def __init__(self, models, caches):
        self.models = models
        self.caches = caches


def build_caching(models=None, caches=None):
    models = models or FakeModels(text="ok")
    caches = caches or FakeCaches()
    provider = GeminiProvider(
        api_key="k", model="gemini-2.5-pro", client=CachingClient(models, caches)
    )
    return provider, models, caches


def test_the_standing_context_is_cached_once_and_referenced_instead_of_resent():
    provider, models, caches = build_caching()
    for _ in range(3):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert len(caches.calls) == 1
    assert caches.calls[0]["config"]["system_instruction"] == BIG_SYSTEM
    assert models.kwargs["config"]["cached_content"] == "cachedContents/abc"
    # Both together is a 400 from the API, and re-sending defeats the point.
    assert "system_instruction" not in models.kwargs["config"]

    provider.complete(system=BIG_SYSTEM + " revised", user="u")
    assert len(caches.calls) == 2

    provider.complete(system="short", user="u")
    assert len(caches.calls) == 2
    assert models.kwargs["config"]["system_instruction"] == "short"


def test_caching_failure_falls_back_inline_and_is_not_retried():
    provider, models, caches = build_caching(caches=FakeCaches(error=RuntimeError("no")))
    for _ in range(3):
        assert provider.complete(system=BIG_SYSTEM, user="u") == "ok"
    assert len(caches.calls) == 1
    assert models.kwargs["config"]["system_instruction"] == BIG_SYSTEM
    assert "cached_content" not in models.kwargs["config"]

    # A client with no caches support at all behaves the same way.
    provider, models = build("ok")
    assert provider.complete(system=BIG_SYSTEM, user="u") == "ok"
    assert models.kwargs["config"]["system_instruction"] == BIG_SYSTEM


def test_an_expired_cache_is_retried_inline_but_model_failures_are_not():
    """A cache can expire mid-exercise and the question must still be
    answered. A truncated or blocked response is not a cache problem, so
    retrying it would pay for the identical failure twice."""

    class ExpiringModels:
        def __init__(self):
            self.configs = []

        def generate_content(self, **kwargs):
            self.configs.append(kwargs["config"])
            if "cached_content" in kwargs["config"]:
                raise RuntimeError("CachedContent not found")
            return type("R", (), {"text": "ok"})()

    models = ExpiringModels()
    provider, _, _ = build_caching(models=models)
    assert provider.complete(system=BIG_SYSTEM, user="u") == "ok"
    assert models.configs[0]["cached_content"] == "cachedContents/abc"
    assert models.configs[1]["system_instruction"] == BIG_SYSTEM
    assert "cached_content" not in models.configs[1]

    # The stale handle is dropped, so the next question uploads a fresh one.
    provider, _, caches = build_caching(models=FakeModels(error=RuntimeError("gone")))
    for _ in range(2):
        with pytest.raises(LLMError):
            provider.complete(system=BIG_SYSTEM, user="u")
    assert len(caches.calls) == 2

    class TruncatedModels:
        count = 0

        def generate_content(self, **kwargs):
            self.count += 1
            return finished(type("F", (), {"name": "MAX_TOKENS"})(), "partial")

    models = TruncatedModels()
    provider, _, _ = build_caching(models=models)
    with pytest.raises(LLMError, match="truncated"):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert models.count == 1


def test_a_quota_limit_raises_usage_limit_and_is_not_retried_inline():
    """Gemini reports an exhausted quota as 429 RESOURCE_EXHAUSTED. That is a
    limit, not a stale cache, so retrying without the cache only spends
    another failed call."""
    error = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded for metric")
    error.code = 429

    class Models:
        count = 0

        def generate_content(self, **kwargs):
            self.count += 1
            raise error

    models = Models()
    provider, _, _ = build_caching(models=models)
    with pytest.raises(UsageLimitError, match="Gemini request failed"):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert models.count == 1


def test_concurrent_questions_create_the_standing_context_cache_once():
    """Rulings run on worker threads, so two questions can arrive at the
    provider together. Racing them into two cache uploads of the same
    context doubles the cost of the biggest request the bot ever makes."""

    class SlowCaches(FakeCaches):
        def create(self, **kwargs):
            time.sleep(0.05)
            return super().create(**kwargs)

    provider, _, caches = build_caching(caches=SlowCaches())
    barrier = threading.Barrier(4)

    def ask():
        barrier.wait()
        provider.complete(system=BIG_SYSTEM, user="u")

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(caches.calls) == 1
