import pytest

from rangecontrol.llm.base import LLMError, Provider
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
    def __init__(self, models):
        self.models = models


def build(text=None, error=None):
    models = FakeModels(text=text, error=error)
    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=FakeClient(models))
    return provider, models


def test_satisfies_provider_protocol():
    provider, _ = build("ok")
    assert isinstance(provider, Provider)


def test_returns_text():
    provider, _ = build("hello")
    assert provider.complete(system="s", user="u") == "hello"


def test_system_prompt_goes_in_system_instruction():
    provider, models = build("ok")
    provider.complete(system="sys", user="usr")
    assert models.kwargs["config"]["system_instruction"] == "sys"
    assert models.kwargs["contents"] == "usr"


def test_schema_sets_json_mime_type_and_response_schema():
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    provider, models = build("{}")
    provider.complete(system="s", user="u", schema=schema)
    assert models.kwargs["config"]["response_mime_type"] == "application/json"
    # Sent sanitized: Gemini 400s on additionalProperties, which Anthropic requires.
    assert models.kwargs["config"]["response_schema"] == {
        "type": "object",
        "properties": {},
        "required": [],
    }


def test_truncated_response_raises():
    class Candidate:
        finish_reason = "MAX_TOKENS"

    class Truncated:
        candidates = [Candidate()]
        text = "a partial document that stops mid-"

    class Models:
        def generate_content(self, **kwargs):
            return Truncated()

    class Client:
        models = Models()

    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=Client())
    with pytest.raises(LLMError) as exc:
        provider.complete(system="s", user="u")
    assert "truncated" in str(exc.value).lower()


def test_stop_finish_reason_still_succeeds():
    class Candidate:
        finish_reason = "STOP"

    class Complete:
        candidates = [Candidate()]
        text = "a complete answer"

    class Models:
        def generate_content(self, **kwargs):
            return Complete()

    class Client:
        models = Models()

    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=Client())
    assert provider.complete(system="s", user="u") == "a complete answer"


def test_safety_finish_reason_raises():
    class Candidate:
        finish_reason = "SAFETY"

    class Blocked:
        candidates = [Candidate()]
        text = "a partial answer that got cut off by the safety filt"

    class Models:
        def generate_content(self, **kwargs):
            return Blocked()

    class Client:
        models = Models()

    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=Client())
    with pytest.raises(LLMError) as exc:
        provider.complete(system="s", user="u")
    assert "safety" in str(exc.value).lower()


def test_missing_text_attribute_raises():
    class NoText:
        candidates = []

    class Models:
        def generate_content(self, **kwargs):
            return NoText()

    class Client:
        models = Models()

    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=Client())
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_empty_response_raises():
    provider, _ = build("   ")
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_none_response_raises():
    provider, _ = build(None)
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_sdk_exception_is_wrapped():
    provider, _ = build(error=RuntimeError("network down"))
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_raising_text_property_becomes_llm_error():
    class Exploding:
        @property
        def text(self):
            raise RuntimeError("malformed candidate list")

    class Models:
        def generate_content(self, **kwargs):
            return Exploding()

    class Client:
        models = Models()

    provider = GeminiProvider(api_key="k", model="gemini-2.5-pro", client=Client())
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_describe_image_sends_inline_part():
    provider, models = build("a diagram")
    assert provider.describe_image(data=b"x", mime_type="image/png", prompt="p") == "a diagram"
    parts = models.kwargs["contents"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert parts[1] == "p"


def test_schema_is_stripped_of_keys_gemini_rejects():
    """Gemini 400s on additionalProperties; Anthropic's strict mode requires it."""
    from rangecontrol.advisor.prompts import RULING_SCHEMA

    provider, models = build("{}")
    provider.complete(system="s", user="u", schema=RULING_SCHEMA)
    sent = models.kwargs["config"]["response_schema"]
    assert "additionalProperties" not in sent
    assert sent["type"] == "object"
    assert set(sent["required"]) == set(RULING_SCHEMA["required"])
    assert sent["properties"]["verdict"]["enum"] == ["APPROVED", "DENIED"]


def test_stripping_does_not_mutate_the_shared_schema():
    """The schema constants are shared with the Anthropic adapter, which needs the key."""
    from rangecontrol.advisor.prompts import RULING_SCHEMA

    provider, _ = build("{}")
    provider.complete(system="s", user="u", schema=RULING_SCHEMA)
    assert RULING_SCHEMA["additionalProperties"] is False


def test_nested_object_schemas_are_stripped_recursively():
    nested = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"inner": {"type": "object", "additionalProperties": False}},
        "required": ["inner"],
    }
    provider, models = build("{}")
    provider.complete(system="s", user="u", schema=nested)
    sent = models.kwargs["config"]["response_schema"]
    assert "additionalProperties" not in sent
    assert "additionalProperties" not in sent["properties"]["inner"]


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


def build_caching(text="ok", error=None, cache_error=None):
    models = FakeModels(text=text, error=error)
    caches = FakeCaches(error=cache_error)
    provider = GeminiProvider(
        api_key="k", model="gemini-2.5-pro", client=CachingClient(models, caches)
    )
    return provider, models, caches


def test_large_context_is_cached_and_referenced_instead_of_resent():
    provider, models, caches = build_caching()
    provider.complete(system=BIG_SYSTEM, user="u")
    assert caches.calls[0]["config"]["system_instruction"] == BIG_SYSTEM
    assert models.kwargs["config"]["cached_content"] == "cachedContents/abc"
    # Both together is a 400 from the API, and re-sending defeats the point.
    assert "system_instruction" not in models.kwargs["config"]


def test_cache_is_created_once_and_reused_across_questions():
    provider, models, caches = build_caching()
    for _ in range(3):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert len(caches.calls) == 1
    assert models.kwargs["config"]["cached_content"] == "cachedContents/abc"


def test_changed_context_creates_a_new_cache():
    provider, _, caches = build_caching()
    provider.complete(system=BIG_SYSTEM, user="u")
    provider.complete(system=BIG_SYSTEM + " revised", user="u")
    assert len(caches.calls) == 2


def test_small_context_is_sent_inline_without_a_cache():
    provider, models, caches = build_caching()
    provider.complete(system="short", user="u")
    assert caches.calls == []
    assert models.kwargs["config"]["system_instruction"] == "short"


def test_caching_failure_falls_back_to_inline_and_still_answers():
    provider, models, _ = build_caching(cache_error=RuntimeError("no caching here"))
    assert provider.complete(system=BIG_SYSTEM, user="u") == "ok"
    assert models.kwargs["config"]["system_instruction"] == BIG_SYSTEM
    assert "cached_content" not in models.kwargs["config"]


def test_caching_is_not_retried_after_it_fails_once():
    provider, _, caches = build_caching(cache_error=RuntimeError("unsupported"))
    for _ in range(3):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert len(caches.calls) == 1


def test_expired_cache_is_retried_inline_rather_than_costing_a_ruling():
    """A cache can expire mid-exercise; the question must still be answered."""

    class ExpiringModels:
        def __init__(self):
            self.configs = []

        def generate_content(self, **kwargs):
            self.configs.append(kwargs["config"])
            if "cached_content" in kwargs["config"]:
                raise RuntimeError("CachedContent not found")
            return type("R", (), {"text": "ok"})()

    models = ExpiringModels()
    caches = FakeCaches()
    provider = GeminiProvider(
        api_key="k", model="gemini-2.5-pro", client=CachingClient(models, caches)
    )
    assert provider.complete(system=BIG_SYSTEM, user="u") == "ok"
    assert models.configs[0]["cached_content"] == "cachedContents/abc"
    assert models.configs[1]["system_instruction"] == BIG_SYSTEM
    assert "cached_content" not in models.configs[1]


def test_stale_handle_is_dropped_so_the_next_question_recreates_it():
    provider, _, caches = build_caching(error=RuntimeError("CachedContent not found"))
    with pytest.raises(LLMError):
        provider.complete(system=BIG_SYSTEM, user="u")
    with pytest.raises(LLMError):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert len(caches.calls) == 2


def test_model_side_failure_is_not_retried():
    """A truncated or blocked response is not a cache problem.

    Retrying it inline would pay for the identical failure a second time, so
    only request-layer failures trigger the fallback.
    """

    class Candidate:
        finish_reason = type("F", (), {"name": "MAX_TOKENS"})()

    class TruncatedModels:
        def __init__(self):
            self.count = 0

        def generate_content(self, **kwargs):
            self.count += 1
            return type("R", (), {"candidates": [Candidate()], "text": "partial"})()

    models = TruncatedModels()
    provider = GeminiProvider(
        api_key="k", model="gemini-2.5-pro", client=CachingClient(models, FakeCaches())
    )
    with pytest.raises(LLMError, match="truncated"):
        provider.complete(system=BIG_SYSTEM, user="u")
    assert models.count == 1


def test_a_client_without_caches_support_still_works():
    """Older SDKs and injected test doubles have no .caches attribute."""
    provider, models = build("ok")
    assert provider.complete(system=BIG_SYSTEM, user="u") == "ok"
    assert models.kwargs["config"]["system_instruction"] == BIG_SYSTEM


def test_concurrent_questions_create_the_standing_context_cache_once():
    """Rulings now run on worker threads, so two questions can arrive at the
    provider together. Racing them into two cache uploads of the same
    context doubles the cost of the biggest request the bot ever makes."""
    import threading
    import time

    class SlowCaches(FakeCaches):
        def create(self, **kwargs):
            time.sleep(0.05)
            return super().create(**kwargs)

    caches = SlowCaches()
    provider = GeminiProvider(
        api_key="k", model="gemini-2.5-pro",
        client=CachingClient(FakeModels(text="ok"), caches),
    )
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


def test_safety_thresholds_are_relaxed_for_range_material():
    provider, models = build("ok")
    provider.complete(system="s", user="u")
    settings = models.kwargs["config"]["safety_settings"]
    assert all(item["threshold"] == "BLOCK_ONLY_HIGH" for item in settings)
    assert any(item["category"] == "HARM_CATEGORY_DANGEROUS_CONTENT" for item in settings)
