"""Read files packaged inside the binary."""

from __future__ import annotations

from rangecontrol.gui.paths import bundle_root


def read_bundled(name: str) -> str:
    path = bundle_root() / "rangecontrol" / name
    if not path.is_file():
        path = bundle_root() / name
    return path.read_text(encoding="utf-8")
