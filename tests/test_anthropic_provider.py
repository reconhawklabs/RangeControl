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


def build(result=None, error=None):
    messages = FakeMessages(result=result, error=error)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5", client=FakeClient(messages))
    return provider, messages


def test_satisfies_provider_protocol():
    provider, _ = build()
    assert isinstance(provider, Provider)


def test_returns_concatenated_text_blocks():
    provider, _ = build(Response([Block("thinking"), Block("text", "hello")]))
    assert provider.complete(system="s", user="u") == "hello"


def test_system_is_sent_as_cacheable_block_list():
    provider, messages = build(Response([Block("text", "ok")]))
    provider.complete(system="big system prompt", user="u")
    system = messages.kwargs["system"]
    assert isinstance(system, list)
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert system[-1]["text"] == "big system prompt"


def test_never_sends_rejected_sampling_parameters():
    provider, messages = build(Response([Block("text", "ok")]))
    provider.complete(system="s", user="u")
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in messages.kwargs


def test_schema_is_passed_via_output_config():
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    provider, messages = build(Response([Block("text", "{}")]))
    provider.complete(system="s", user="u", schema=schema)
    assert messages.kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}
    }


def test_refusal_stop_reason_raises():
    provider, _ = build(Response([], stop_reason="refusal"))
    with pytest.raises(LLMError) as exc:
        provider.complete(system="s", user="u")
    assert "refus" in str(exc.value).lower()


def test_truncated_response_raises():
    provider, _ = build(Response([Block("text", "partial")], stop_reason="max_tokens"))
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_empty_text_raises():
    provider, _ = build(Response([Block("thinking")]))
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_sdk_exception_is_wrapped_in_llm_error():
    provider, _ = build(error=RuntimeError("connection reset"))
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_malformed_response_shape_raises_llm_error():
    provider, _ = build(object())  # no .content, no .stop_reason
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_none_response_raises_llm_error():
    provider, _ = build(None)
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_large_max_tokens_uses_the_streaming_path():
    provider, messages = build(Response([Block("text", "a long generated document")]))
    result = provider.complete(system="s", user="u", max_tokens=STREAM_THRESHOLD)
    assert result == "a long generated document"
    assert messages.stream_kwargs is not None
    assert messages.stream_kwargs["max_tokens"] == STREAM_THRESHOLD
    assert messages.kwargs is None  # create() was never called


def test_small_max_tokens_uses_the_non_streaming_path():
    provider, messages = build(Response([Block("text", "ok")]))
    provider.complete(system="s", user="u", max_tokens=STREAM_THRESHOLD - 1)
    assert messages.kwargs is not None
    assert messages.stream_kwargs is None  # stream() was never called


def test_streaming_path_still_wraps_sdk_failures():
    provider, _ = build(error=RuntimeError("connection reset"), result=None)
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u", max_tokens=STREAM_THRESHOLD)


def test_describe_image_sends_base64_image_block():
    provider, messages = build(Response([Block("text", "a diagram")]))
    assert provider.describe_image(data=b"bytes", mime_type="image/png", prompt="p") == "a diagram"
    content = messages.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
