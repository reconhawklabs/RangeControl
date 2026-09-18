"""Real, tiny images for tests: the vision path decodes before it uploads."""

from __future__ import annotations

import io
from pathlib import Path


def png_bytes(size: tuple[int, int] = (120, 80), colour=(200, 30, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def write_png(path: Path, size: tuple[int, int] = (120, 80)) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(size))
    return path
