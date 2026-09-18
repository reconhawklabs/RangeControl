import pytest

from rangecontrol.llm.base import LLMError, Provider
from tests.support.stub_provider import StubProvider


def test_stub_satisfies_provider_protocol():
    assert isinstance(StubProvider(), Provider)


def test_returns_queued_completions_in_order():
    stub = StubProvider(completions=["first", "second"])
    assert stub.complete(system="s", user="u") == "first"
    assert stub.complete(system="s", user="u") == "second"


def test_records_call_arguments():
    stub = StubProvider(completions=["x"])
    stub.complete(system="sys", user="usr", schema={"type": "object"}, max_tokens=42)
    assert stub.calls == [
        {
            "system": "sys",
            "user": "usr",
            "schema": {"type": "object"},
            "max_tokens": 42,
            "effort": None,
        }
    ]


def test_raises_when_completions_exhausted():
    stub = StubProvider(completions=["only"])
    stub.complete(system="s", user="u")
    with pytest.raises(AssertionError):
        stub.complete(system="s", user="u")


def test_configured_error_is_raised():
    stub = StubProvider(error=LLMError("boom"))
    with pytest.raises(LLMError):
        stub.complete(system="s", user="u")


def test_describe_image_returns_configured_text_and_records_call():
    stub = StubProvider(image_text="a network diagram")
    assert stub.describe_image(data=b"\x89PNG", mime_type="image/png", prompt="describe") == (
        "a network diagram"
    )
    assert stub.image_calls[0]["mime_type"] == "image/png"
