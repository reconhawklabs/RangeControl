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
    # Image budgets sit above STREAM_THRESHOLD, so the request streams.
    content = messages.stream_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"


def test_request_failures_carry_the_sdk_message():
    """'BadRequestError' alone tells an operator nothing; the SDK's message
    ('prompt is too long: ...') is what they need to act on."""
    class TooLong(Exception):
        message = "prompt is too long: 250000 tokens > 200000 maximum"

    class Messages:
        def create(self, **kwargs):
            raise TooLong("prompt is too long: 250000 tokens > 200000 maximum")

        def stream(self, **kwargs):
            raise TooLong("prompt is too long")

    class Client:
        messages = Messages()

    provider = AnthropicProvider(api_key="k", model="m", client=Client())
    with pytest.raises(LLMError, match="prompt is too long"):
        provider.complete(system="s", user="u")


def test_effort_is_sent_inside_output_config():
    provider, messages = build(Response([Block("text", "{}")]))
    provider.complete(system="s", user="u", effort="medium")
    assert messages.kwargs["output_config"]["effort"] == "medium"


def test_effort_and_schema_share_output_config():
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    provider, messages = build(Response([Block("text", "{}")]))
    provider.complete(system="s", user="u", schema=schema, effort="high")
    config = messages.kwargs["output_config"]
    assert config["effort"] == "high"
    assert config["format"]["schema"] == schema


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
                raise Rejects(self.message)
            return Response([Block("text", "ok")])

        def stream(self, **kwargs):
            raise AssertionError("not used at this max_tokens")

    Messages.message = Rejects.message
    messages = Messages()

    class Client:
        pass

    client = Client()
    client.messages = messages
    provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5", client=client)

    assert provider.complete(system="s", user="u", effort="medium") == "ok"
    assert len(messages.calls) == 2
    assert "output_config" not in messages.calls[1]

    provider.complete(system="s", user="u", effort="medium")
    assert len(messages.calls) == 3  # learned: no effort, no retry


def test_describe_image_gets_a_budget_that_survives_thinking():
    """Observed on a real range PDF: a 4000 cap was consumed by reasoning
    before a single word of the description came back."""
    provider, messages = build(Response([Block("text", "a diagram")]))
    provider.describe_image(data=b"x", mime_type="image/png", prompt="p", effort="medium")
    assert messages.stream_kwargs is not None  # large budgets go through streaming
    assert messages.stream_kwargs["max_tokens"] >= 16000
    assert messages.stream_kwargs["output_config"]["effort"] == "medium"


class _BetaMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None
        self.stream_kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return _StreamContext(self.response)


class _StreamContext:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._response


def _beta_client(response):
    client = type("Client", (), {})()
    client.messages = _BetaMessages(response)
    client.beta = type("Beta", (), {})()
    client.beta.messages = _BetaMessages(response)
    return client


def test_refusal_fallbacks_are_requested_on_the_beta_surface():
    """A classifier false positive on range material must be re-run on
    Anthropic's recommended fallback model server-side, not handed to the
    blue team as an outage."""
    client = _beta_client(Response([Block("text", "ok")]))
    provider = AnthropicProvider(api_key="k", model="claude-opus-5", client=client)
    assert provider.complete(system="s", user="u") == "ok"
    sent = client.beta.messages.kwargs
    assert sent["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent["betas"]
    assert client.messages.kwargs is None


def test_a_platform_that_rejects_fallbacks_is_retried_without_them_once():
    class Rejects(Exception):
        status_code = 400
        message = "fallbacks: Extra inputs are not permitted"

    client = _beta_client(Response([Block("text", "ok")]))

    def refuse(**kwargs):
        raise Rejects(Rejects.message)

    client.beta.messages.create = refuse
    provider = AnthropicProvider(api_key="k", model="claude-opus-5", client=client)
    assert provider.complete(system="s", user="u") == "ok"
    assert "fallbacks" not in client.messages.kwargs
    provider.complete(system="s", user="u")  # no second attempt on the beta path


def test_a_refusal_names_its_category_for_the_white_cell():
    details = type("Details", (), {"category": "cyber", "explanation": "Declined."})()
    response = Response([], stop_reason="refusal")
    response.stop_details = details
    provider, _ = build(response)
    with pytest.raises(LLMError, match="category=cyber"):
        provider.complete(system="s", user="u")
