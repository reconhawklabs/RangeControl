import pytest

from rangecontrol.llm.anthropic_provider import STREAM_THRESHOLD, AnthropicProvider
from rangecontrol.llm.base import LLMError, Provider


class FakeStream:
    """Stands in for the SDK's ``messages.stream(...)`` context manager."""

    def __init__(self, result, error):
        self._result = result
        self._error = error

    def __enter__(self):
        if self._error:
            raise self._error
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._result


class FakeMessages:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.kwargs = None
        self.stream_kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.result

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return FakeStream(self.result, self.error)


class FakeClient:
    """No ``beta`` attribute, so requests go to the plain messages surface."""

    def __init__(self, messages):
        self.messages = messages


class Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def build(result=None, error=None):
    messages = FakeMessages(result=result, error=error)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5", client=FakeClient(messages))
    return provider, messages


def test_request_carries_schema_effort_and_a_cacheable_system_prompt():
    provider, messages = build(Response([Block("thinking"), Block("text", "{}")]))
    assert isinstance(provider, Provider)

    # Only text blocks come back; thinking blocks are never part of the answer.
    assert provider.complete(system="big system", user="u", schema=SCHEMA, effort="high") == "{}"

    sent = messages.kwargs
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "high",
    }
    assert sent["system"][-1] == {
        "type": "text",
        "text": "big system",
        "cache_control": {"type": "ephemeral"},
    }
    # Parameters the newest models reject with a 400 on every call.
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in sent


def test_every_unusable_response_fails_closed_with_llm_error():
    """Anything short of a complete text answer must raise, never be returned
    as if it were a ruling."""
    cases = {
        "truncated": Response([Block("text", "partial")], stop_reason="max_tokens"),
        "thinking only": Response([Block("thinking")]),
        "blank text": Response([Block("text", "   ")]),
        "wrong shape": object(),  # no .content, no .stop_reason
        "none": None,
    }
    escaped = []
    for label, response in cases.items():
        provider, _ = build(response)
        try:
            provider.complete(system="s", user="u")
        except LLMError:
            continue
        escaped.append(label)
    assert escaped == []


def test_a_refusal_raises_and_names_its_category_for_the_white_cell():
    provider, _ = build(Response([], stop_reason="refusal"))
    with pytest.raises(LLMError, match="refus"):
        provider.complete(system="s", user="u")

    response = Response([Block("text", "partial")], stop_reason="refusal")
    response.stop_details = type("D", (), {"category": "cyber", "explanation": "Declined."})()
    provider, _ = build(response)
    with pytest.raises(LLMError, match="category=cyber"):
        provider.complete(system="s", user="u")


def test_sdk_failures_are_wrapped_with_the_sdk_message_on_both_paths():
    """'BadRequestError' alone tells an operator nothing; the SDK's message
    ('prompt is too long: ...') is what they need to act on."""

    class TooLong(Exception):
        message = "prompt is too long: 250000 tokens > 200000 maximum"

    for max_tokens in (2000, STREAM_THRESHOLD):
        provider, _ = build(error=TooLong(TooLong.message))
        with pytest.raises(LLMError, match="prompt is too long"):
            provider.complete(system="s", user="u", max_tokens=max_tokens)


def test_large_budgets_stream_and_small_ones_do_not():
    provider, messages = build(Response([Block("text", "a long document")]))
    assert provider.complete(system="s", user="u", max_tokens=STREAM_THRESHOLD) == "a long document"
    assert messages.stream_kwargs["max_tokens"] == STREAM_THRESHOLD
    assert messages.kwargs is None

    provider, messages = build(Response([Block("text", "ok")]))
    provider.complete(system="s", user="u", max_tokens=STREAM_THRESHOLD - 1)
    assert messages.kwargs is not None
    assert messages.stream_kwargs is None


def test_describe_image_sends_the_image_with_a_budget_that_survives_thinking():
    """Observed on a real range PDF: a 4000 cap was consumed by reasoning
    before a single word of the description came back."""
    provider, messages = build(Response([Block("text", "a diagram")]))
    result = provider.describe_image(
        data=b"bytes", mime_type="image/png", prompt="p", effort="medium"
    )
    assert result == "a diagram"
    sent = messages.stream_kwargs
    assert sent["max_tokens"] >= 16000
    assert sent["output_config"]["effort"] == "medium"
    image = sent["messages"][0]["content"][0]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"


def test_a_model_that_rejects_effort_is_retried_without_it_once():
    """Haiku 4.5 returns a 400 for output_config.effort. That must cost one
    retry on the first call, not a refusal on every question."""

    class Rejects(Exception):
        status_code = 400
        message = "output_config.effort: Extra inputs are not permitted"

    class Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if "effort" in kwargs.get("output_config", {}):
                raise Rejects(Rejects.message)
            return Response([Block("text", "ok")])

    messages = Messages()
    provider = AnthropicProvider(
        api_key="k", model="claude-haiku-4-5", client=FakeClient(messages)
    )

    assert provider.complete(system="s", user="u", effort="medium") == "ok"
    assert len(messages.calls) == 2
    assert "output_config" not in messages.calls[1]

    provider.complete(system="s", user="u", effort="medium")
    assert len(messages.calls) == 3  # learned: no effort, no retry


def test_refusal_fallbacks_are_requested_and_dropped_once_if_rejected():
    """A classifier false positive on range material must be re-run on
    Anthropic's recommended fallback model server-side, not handed to the
    blue team as an outage. A platform that rejects the parameter costs one
    retry, not every ruling."""

    def beta_client():
        client = FakeClient(FakeMessages(result=Response([Block("text", "ok")])))
        client.beta = FakeClient(FakeMessages(result=Response([Block("text", "ok")])))
        return client

    client = beta_client()
    provider = AnthropicProvider(api_key="k", model="claude-opus-5", client=client)
    assert provider.complete(system="s", user="u") == "ok"
    sent = client.beta.messages.kwargs
    assert sent["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent["betas"]
    assert client.messages.kwargs is None

    class Rejects(Exception):
        status_code = 400
        message = "fallbacks: Extra inputs are not permitted"

    client = beta_client()
    client.beta.messages.error = Rejects(Rejects.message)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5", client=client)
    assert provider.complete(system="s", user="u") == "ok"
    assert "fallbacks" not in client.messages.kwargs

    client.beta.messages.kwargs = None
    provider.complete(system="s", user="u")
    assert client.beta.messages.kwargs is None  # not asked again
