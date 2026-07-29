"""Read and write .env without destroying what the user put there.

The GUI saves on every edit, so this runs constantly. It must never drop a
key it does not recognise: LLM_GATE_MODEL and RANGE_DIR are deliberately
absent from the form, and a user who set one by hand would otherwise watch it
disappear the first time they touched a dropdown.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

ENV_FILENAME = ".env"

_NEEDS_QUOTING = set(' \t"\'#\n\r')


def _is_data_line(stripped: str) -> bool:
    """Check if a line contains a key=value pair (not a comment or blank)."""
    return bool(stripped and not stripped.startswith("#") and "=" in stripped)


def read_env(path: Path) -> dict[str, str]:
    """Parse a .env file. A missing file reads as empty, not an error."""
    path = Path(path)
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not _is_data_line(stripped):
            continue
        key, _, raw = stripped.partition("=")
        values[key.strip()] = _unquote(raw.strip())
    return values


_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r"}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            # For double-quoted values, unescape escape sequences in a single pass
            # to avoid disambiguating ambiguous sequences like a literal backslash-n
            inner = value[1:-1]
            out = []
            i = 0
            while i < len(inner):
                if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] in _UNESCAPES:
                    out.append(_UNESCAPES[inner[i + 1]])
                    i += 2
                else:
                    out.append(inner[i])
                    i += 1
            return "".join(out)
        else:
            # For single-quoted values, just strip quotes (user wrote by hand)
            return value[1:-1]
    return value


def _quote(value: str) -> str:
    if value and not (_NEEDS_QUOTING & set(value)):
        return value
    escaped = (value
        .replace("\\", "\\\\")       # backslash first: \ -> \\
        .replace("\n", "\\n")        # newline: \n -> \\n
        .replace("\r", "\\r")        # carriage return: \r -> \\r
        .replace('"', '\\"'))        # quote: " -> \"
    return f'"{escaped}"'


def update_env(path: Path, changes: dict[str, str]) -> None:
    """Apply ``changes`` in place, preserving comments, order, and unknown keys.

    Written to a temp file and renamed. A plain write truncates first, so an
    interrupted save would leave a .env holding half an API key — recoverable
    only by retyping every secret.
    """
    path = Path(path)
    remaining = dict(changes)
    lines: list[str] = []

    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not _is_data_line(stripped):
                lines.append(line)
                continue
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                lines.append(f"{key}={_quote(remaining.pop(key))}")
            else:
                lines.append(line)

    for key, value in remaining.items():
        lines.append(f"{key}={_quote(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
