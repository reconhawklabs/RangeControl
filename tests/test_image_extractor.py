import pytest

from rangecontrol.ingest.extractors.image import MAX_IMAGE_BYTES, extract_image
from tests.support.images import write_png
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider


def write_image(tmp_path, name="diagram.png", size=64):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (size, size), (200, 30, 30)).save(path)
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


def _png(tmp_path, name="d.png", size=(120, 80), noisy=False):
    from PIL import Image
    import os

    if noisy:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (200, 30, 30))
    path = tmp_path / name
    image.save(path)
    return path


def test_bmp_is_converted_to_png_before_the_provider_sees_it(tmp_path):
    """Neither vision API accepts image/bmp, so a .bmp diagram used to fail
    with an opaque 400 on every run. It is converted, not refused."""
    from PIL import Image

    path = tmp_path / "topology.bmp"
    Image.new("RGB", (100, 60), (0, 0, 200)).save(path)
    stub = StubProvider(image_text="ok")
    extract_image(path, stub)
    assert stub.image_calls[0]["mime_type"] == "image/png"


def test_oversized_dimensions_are_downscaled_not_refused(tmp_path):
    from PIL import Image
    import io

    path = _png(tmp_path, "wall.png", size=(9000, 300))
    stub = StubProvider(image_text="ok")
    extract_image(path, stub)
    sent = stub.image_calls[0]
    assert sent["mime_type"] in {"image/png", "image/jpeg"}


def test_an_image_above_the_byte_limit_is_shrunk_under_it(tmp_path):
    path = _png(tmp_path, "photo.png", size=(1800, 1800), noisy=True)
    assert path.stat().st_size > MAX_IMAGE_BYTES  # random noise does not compress
    stub = StubProvider(image_text="ok")
    extract_image(path, stub)
    assert stub.image_calls[0]["size"] <= MAX_IMAGE_BYTES


def test_a_file_that_is_not_an_image_raises_value_error(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG" + b"\x00" * 64)
    with pytest.raises(ValueError):
        extract_image(path, StubProvider())
