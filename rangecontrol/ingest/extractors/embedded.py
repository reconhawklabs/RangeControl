"""Describe the pictures embedded inside documents.

A network diagram pasted into a Word brief, a slide deck, or an exported PDF
is exactly the material an adjudicator needs, and text extraction alone
drops it without a trace. Each picture goes through the same vision path as
a standalone image file, with two bounds: icons and bullets are skipped, and
the number of vision calls per document is capped so a 200-slide deck cannot
turn ingest into a surprise bill.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence

from rangecontrol.ingest.extractors import image as image_extractor
from rangecontrol.llm.base import Provider

logger = logging.getLogger(__name__)

# Vision calls per document. Beyond this the remaining pictures are counted
# in a note rather than described, so the operator can see what was left out.
MAX_EMBEDDED_IMAGES = 25

# Anything with an edge below this is a logo, bullet, or icon, not a diagram.
MIN_EMBEDDED_EDGE = 48

# Marks the trailing block of descriptions in PDF and DOCX output. The
# per-file truncation in discovery keeps everything after it, so a paid
# description is never the part that gets cut.
EMBEDDED_IMAGES_HEADER = "--- embedded images ---"


def _is_tiny(data: bytes) -> bool:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as picture:
            width, height = picture.size
    except Exception:  # noqa: BLE001 - undecodable is handled by describe()
        return False
    return min(width, height) < MIN_EMBEDDED_EDGE


def describe_embedded(
    images: Sequence[tuple[str, bytes]], provider: Provider
) -> list[str]:
    """Return one entry per input image, in order.

    An entry is the description prefixed with its label, a bracketed note
    when the picture could not be described, or an empty string when it was
    skipped (too small, or past the per-document cap). A single failed
    picture never fails the document: the text around it is still range
    material.
    """
    results: list[str] = []
    attempted = 0
    for label, data in images:
        if attempted >= MAX_EMBEDDED_IMAGES or _is_tiny(data):
            results.append("")
            continue
        attempted += 1
        try:
            text = image_extractor.describe(data, provider)
        except ValueError as exc:
            logger.warning("embedded image %s: %s", label, exc)
            results.append(f"[{label}: could not be described: {exc}]")
            continue
        results.append(f"[{label}]\n{text}")
    return results


def cap_note(images: Sequence[tuple[str, bytes]]) -> str:
    """The trailing note naming how many pictures fell past the cap."""
    eligible = sum(1 for _, data in images if not _is_tiny(data))
    excess = eligible - MAX_EMBEDDED_IMAGES
    if excess <= 0:
        return ""
    return (
        f"[{excess} more image(s) in this document were not described: "
        f"limit of {MAX_EMBEDDED_IMAGES} per document. Export the important "
        "ones as separate image files if they matter.]"
    )
