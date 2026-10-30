"""Build rangecontrol/english_words.txt.gz from the Moby word list.

The reply screen uses it to tell a heading set in capitals ("CHANGE REQUEST
FORM") from a range name set in capitals ("WHEARTLAND"): only English words
are dropped from the range vocabulary. See range_vocabulary in
rangecontrol/advisor/screen.py.

Source: the Moby Project word list (public domain), shipped by Fedora's
`words` package and Debian's `wamerican-huge` as /usr/share/dict/words.
Kept: entries of 3 to 14 lowercase ASCII letters. Capitalised entries are
proper nouns, which are exactly what must stay range terms.

A larger source list drops more terms from the screen, which is the unsafe
direction. The shipped list came from Fedora words-3.0-63 (source sha256
ce374089bf2b4d475aaceae5c59561e2ce22c7375f1be891d02e1d7e2ada9418; 335,370
words, output sha256
431d7fc1f9a513574d63e72e4289585b24cce7b0b6f66f96c4c356e584f4c033);
the script prints both hashes so a rebuild can be compared.

    python scripts/build_english_words.py [/usr/share/dict/words]
"""

from __future__ import annotations

import gzip
import hashlib
import re
import sys
from pathlib import Path

SOURCE = Path("/usr/share/dict/words")
TARGET = Path(__file__).resolve().parent.parent / "rangecontrol" / "english_words.txt.gz"
_WORD = re.compile(r"[a-z]{3,14}")


def build(source: Path, target: Path) -> int:
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    words = sorted({line.strip() for line in lines if _WORD.fullmatch(line.strip())})
    # mtime=0 so the same source always produces the same bytes.
    data = gzip.compress("\n".join(words).encode("ascii") + b"\n", 9, mtime=0)
    target.write_bytes(data)
    return len(words)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else SOURCE
    if not source.is_file():
        print(f"Error: {source} not found. Install the 'words' package.", file=sys.stderr)
        return 1
    count = build(source, TARGET)
    print(f"Wrote {count} words to {TARGET}")
    print(f"source sha256 {_sha256(source)}")
    print(f"output sha256 {_sha256(TARGET)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
