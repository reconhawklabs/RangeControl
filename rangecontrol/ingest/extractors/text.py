"""Plaintext and best-effort binary extraction."""

from __future__ import annotations

import re
from pathlib import Path

from rangecontrol.llm.base import Provider

_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\t\n\r]{4,}")

# Byte-order marks that reliably identify UTF-16 content. Without a BOM,
# blindly attempting a UTF-16 decode of arbitrary bytes is unsafe: most byte
# sequences are "valid" UTF-16 code units, so ASCII/UTF-8 text run through a
# blind UTF-16 attempt can decode without error into fluent-looking mojibake —
# exactly the "failure that looks like a success" shape this project has hit
# before, just one layer up. Gating on the BOM keeps the UTF-16 path to files
# that actually declare themselves as UTF-16. Decoding with the generic
# "utf-16" codec name (rather than an explicit -le/-be variant) both
# auto-detects the byte order from the BOM and strips it from the result.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

# Above this fraction of replacement characters (U+FFFD) in a UTF-8
# errors="replace" decode, the file is treated as unusable rather than
# mangled-but-accepted.
_MAX_REPLACEMENT_RATIO = 0.02


def extract_text(path: Path, provider: Provider) -> str:
    """Decode a text file, refusing to pass mangled bytes off as a clean read.

    Tries UTF-8 strictly first (the common case). On failure, tries UTF-16 —
    but only when a byte-order mark says the file actually is UTF-16, since a
    blind UTF-16 attempt on non-UTF-16 bytes can "succeed" with garbage and
    never raise. If neither matches, decodes UTF-8 with replacement and
    measures how much of the result is U+FFFD: a handful of stray bad bytes
    (a smart quote from a different codepage, say) is tolerated, but a file
    that is mostly undecodable as UTF-8 is not text in any encoding we
    support and raises rather than being handed to the adjudicating model as
    if it were.

    Deliberately NOT falling back to Latin-1: Latin-1 decodes any byte
    sequence without ever raising, so it produces zero U+FFFD regardless of
    input — it would silently shred every valid multi-byte UTF-8 character
    into mojibake with no signal to catch it, which is strictly worse than
    what it replaced. `errors="replace"` already does the right thing: it
    preserves every valid character and marks only the genuinely bad bytes.
    """
    data = path.read_bytes()

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    if data.startswith(_UTF16_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass  # BOM present but the body itself is corrupt.

    lossy = data.decode("utf-8", errors="replace")
    if lossy:
        ratio = lossy.count("�") / len(lossy)
        if ratio > _MAX_REPLACEMENT_RATIO:
            raise ValueError(
                f"{path}: could not decode as text "
                f"({ratio:.0%} replacement characters after UTF-8/UTF-16)"
            )
    return lossy


def extract_fallback(path: Path, provider: Provider) -> str:
    """Pull printable runs out of an unrecognised file type."""
    data = path.read_bytes()
    runs = [m.group().decode("ascii", errors="replace") for m in _PRINTABLE_RUN.finditer(data)]
    text = "\n".join(runs).strip()
    if not text:
        raise ValueError("no readable text found")
    return text
