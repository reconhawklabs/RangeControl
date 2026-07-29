"""Vision-based extraction so network diagrams and screenshots become text."""

from __future__ import annotations

from pathlib import Path

from rangecontrol.llm.base import LLMError, Provider

MAX_IMAGE_BYTES = 5 * 1024 * 1024

IMAGE_PROMPT = (
    "This image is part of a cyber security range's documentation. "
    "Describe everything it conveys in plain text so a reader who cannot see it "
    "loses nothing: network topology, trust boundaries, device roles, arrows and "
    "their direction, and any tabular data. Transcribe every visible label, "
    "hostname, address, port, and annotation verbatim. Do not summarise, "
    "interpret, or omit detail."
)

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


def extract_image(path: Path, provider: Provider) -> str:
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"image is {len(data)} bytes, above the {MAX_IMAGE_BYTES} byte limit"
        )

    mime_type = _MIME_TYPES.get(path.suffix.lower())
    if mime_type is None:
        raise ValueError(f"unsupported image type: {path.suffix}")

    try:
        description = provider.describe_image(
            data=data, mime_type=mime_type, prompt=IMAGE_PROMPT
        )
    except LLMError as exc:
        raise ValueError(f"vision extraction failed: {exc}") from exc

    if not description.strip():
        raise ValueError("vision extraction returned an empty description")
    return description.strip()
