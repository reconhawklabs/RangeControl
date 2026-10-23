import os

import pytest
from PIL import Image

from rangecontrol.ingest.extractors.image import MAX_IMAGE_BYTES, extract_image
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider


def write_image(tmp_path, name="diagram.png", size=(64, 64), noisy=False):
    if noisy:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (200, 30, 30))
    path = tmp_path / name
    image.save(path)
    return path


def test_returns_the_vision_description_of_verbatim_labels(tmp_path):
    stub = StubProvider(image_text="Two subnets joined by a firewall.")
    result = extract_image(write_image(tmp_path, "shot.jpg"), stub)
    assert result == "Two subnets joined by a firewall."
    sent = stub.image_calls[0]
    assert sent["mime_type"] == "image/jpeg"
    prompt = sent["prompt"].lower()
    assert "verbatim" in prompt or "exactly" in prompt


def test_images_the_api_would_reject_are_converted_not_refused(tmp_path):
    """Neither vision API accepts image/bmp, very wide images, or files over
    the byte limit; each used to fail with an opaque 400 on every run."""
    bmp = StubProvider(image_text="ok")
    extract_image(write_image(tmp_path, "topology.bmp", size=(100, 60)), bmp)
    assert bmp.image_calls[0]["mime_type"] == "image/png"

    wide = StubProvider(image_text="ok")
    extract_image(write_image(tmp_path, "wall.png", size=(9000, 300)), wide)
    assert wide.image_calls[0]["mime_type"] in {"image/png", "image/jpeg"}

    photo = write_image(tmp_path, "photo.png", size=(1800, 1800), noisy=True)
    assert photo.stat().st_size > MAX_IMAGE_BYTES  # random noise does not compress
    heavy = StubProvider(image_text="ok")
    extract_image(photo, heavy)
    assert heavy.image_calls[0]["size"] <= MAX_IMAGE_BYTES


def test_unusable_images_and_failed_descriptions_raise_value_error(tmp_path):
    good = write_image(tmp_path)
    junk = tmp_path / "huge.png"
    junk.write_bytes(b"\x00" * (MAX_IMAGE_BYTES + 1))
    not_an_image = tmp_path / "broken.png"
    not_an_image.write_bytes(b"\x89PNG" + b"\x00" * 64)

    cases = [
        (junk, StubProvider()),
        (not_an_image, StubProvider()),
        (good, StubProvider(error=LLMError("vision unavailable"))),
        (good, StubProvider(image_text="   ")),
    ]
    for path, provider in cases:
        with pytest.raises(ValueError):
            extract_image(path, provider)
