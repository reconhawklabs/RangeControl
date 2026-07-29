import pytest

from rangecontrol.ingest.extractors.image import MAX_IMAGE_BYTES, extract_image
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider


def write_image(tmp_path, name="diagram.png", size=64):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG" + b"\x00" * size)
    return path


def test_returns_vision_description(tmp_path):
    stub = StubProvider(image_text="Two subnets joined by a firewall.")
    assert extract_image(write_image(tmp_path), stub) == "Two subnets joined by a firewall."


def test_sends_correct_mime_type(tmp_path):
    stub = StubProvider(image_text="ok")
    extract_image(write_image(tmp_path, "shot.jpg"), stub)
    assert stub.image_calls[0]["mime_type"] == "image/jpeg"


def test_prompt_asks_for_verbatim_labels(tmp_path):
    stub = StubProvider(image_text="ok")
    extract_image(write_image(tmp_path), stub)
    prompt = stub.image_calls[0]["prompt"].lower()
    assert "verbatim" in prompt or "exactly" in prompt


def test_oversized_image_raises_value_error(tmp_path):
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x00" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError):
        extract_image(path, StubProvider())


def test_provider_failure_becomes_value_error(tmp_path):
    stub = StubProvider(error=LLMError("vision unavailable"))
    with pytest.raises(ValueError):
        extract_image(write_image(tmp_path), stub)


def test_empty_description_raises_value_error(tmp_path):
    stub = StubProvider(image_text="   ")
    with pytest.raises(ValueError):
        extract_image(write_image(tmp_path), stub)
