"""Vision-based extraction so network diagrams and screenshots become text.

Every image is normalised before it reaches a provider: the two vision APIs
accept a narrow set of formats and sizes, and a diagram refused with an
opaque 400 is a diagram the adjudicator never sees.
"""

from __future__ import annotations

import io
from pathlib import Path

from rangecontrol.llm.base import EFFORT_MEDIUM, LLMError, Provider

# Both providers cap a single image at 5 MB; Anthropic also caps either
# edge at 8000 px. Anything over is downscaled and re-encoded, never refused.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_EDGE = 8000

# A guard on what is read and decoded at all: a multi-hundred-megabyte
# "image" is not a diagram, whatever its extension says.
MAX_INPUT_BYTES = 64 * 1024 * 1024

IMAGE_PROMPT = (
    "This image is part of the exercise control staff's documentation for a "
    "sanctioned cyber security training range. "
    "Describe everything it conveys in plain text so a reader who cannot see it "
    "loses nothing: network topology, trust boundaries, device roles, arrows and "
    "their direction, and any tabular data. Transcribe every visible label, "
    "hostname, address, port, and annotation verbatim. Do not summarise, "
    "interpret, or omit detail."
)

# Formats both vision APIs accept as-is. Everything else (BMP, GIF, TIFF,
# ICO, ...) is re-encoded as PNG: Gemini rejects GIF and neither takes BMP.
_PASSTHROUGH = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

# Longest-edge targets tried in order until the result fits the byte cap.
# 4000 px keeps small labels on a dense diagram legible; the provider
# downsamples further itself if it wants to.
_EDGES = (4000, 3000, 2000, 1400, 1000)
_JPEG_QUALITY = 88


def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Return ``(bytes, mime_type)`` acceptable to either vision API.

    Pass-through when the file is already PNG/JPEG/WebP within limits, so a
    byte-identical upload keeps its cached description. Otherwise decode,
    downscale if needed, and re-encode: PNG first (lossless, right for
    diagrams), JPEG when PNG cannot get under the byte cap (photos).

    Raises ValueError when the bytes are not a decodable image.
    """
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(
            f"image is {len(data)} bytes, above the {MAX_INPUT_BYTES} byte input limit"
        )
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            mime = _PASSTHROUGH.get((image.format or "").upper())
            width, height = image.size
            if (
                mime is not None
                and len(data) <= MAX_IMAGE_BYTES
                and max(width, height) <= MAX_IMAGE_EDGE
            ):
                return data, mime
            return _reencode(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"could not decode image: {type(exc).__name__}: {exc}") from exc


def _reencode(image) -> tuple[bytes, str]:
    from PIL import Image

    # First frame only for animations; a GIF walkthrough is out of scope.
    if getattr(image, "n_frames", 1) > 1:
        image.seek(0)
    has_alpha = "A" in image.getbands() or image.mode == "P"
    for edge in _EDGES:
        frame = image.copy()
        frame.thumbnail((edge, edge), Image.LANCZOS)
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
            buffer = io.BytesIO()
            if fmt == "PNG":
                frame.convert("RGBA" if has_alpha else "RGB").save(
                    buffer, "PNG", optimize=True
                )
            else:
                frame.convert("RGB").save(buffer, "JPEG", quality=_JPEG_QUALITY)
            encoded = buffer.getvalue()
            if len(encoded) <= MAX_IMAGE_BYTES:
                return encoded, mime
    raise ValueError("image could not be reduced below the provider size limit")


def describe(data: bytes, provider: Provider) -> str:
    """Normalise ``data`` and ask the provider to describe it.

    Raises ValueError for an undecodable image, an empty description, or a
    provider failure, so callers treat all three as "unreadable".
    """
    prepared, mime_type = prepare_image(data)
    try:
        description = provider.describe_image(
            data=prepared,
            mime_type=mime_type,
            prompt=IMAGE_PROMPT,
            # Transcription, not deduction: the value is in the output
            # tokens, and deep reasoning over a diagram adds little.
            effort=EFFORT_MEDIUM,
        )
    except LLMError as exc:
        raise ValueError(f"vision extraction failed: {exc}") from exc

    if not description.strip():
        raise ValueError("vision extraction returned an empty description")
    return description.strip()


def extract_image(path: Path, provider: Provider) -> str:
    return describe(path.read_bytes(), provider)
