"""Walk the resources directory and reduce every file to text.

A file that cannot be read is recorded as an unreadable document and reported
at startup. One corrupt PDF must never stop an exercise.
"""

from __future__ import annotations

from pathlib import Path

from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.corpus import Corpus, ExtractedDoc
from rangecontrol.ingest.extractors.embedded import EMBEDDED_IMAGES_HEADER
from rangecontrol.ingest.extractors.registry import cache_version, extractor_for
from rangecontrol.llm.base import Provider


def discover(resources_dir: Path) -> tuple[Path, ...]:
    """Return every non-hidden file under ``resources_dir``, sorted."""
    root = Path(resources_dir)
    if not root.is_dir():
        return ()

    found = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]
    # Sort on the POSIX relative path so ordering is deterministic and identical
    # across platforms. Order itself carries no meaning — only stability does,
    # so the ingest report and the model context read the same way every run.
    return tuple(sorted(found, key=lambda p: p.relative_to(root).as_posix()))


# Per-file ceiling on what reaches the model context. Range.md plus the whole
# corpus goes into every ruling, and one packet log or SIEM export can be
# larger than any model's window on its own; the API then fails the call
# with a 400 nobody can act on. The head is kept, the cut is marked, and the
# ingest report names the file. The cache keeps the full text, so raising
# this later costs no re-extraction.
MAX_DOC_CHARS = 300_000

_TRUNCATION_NOTE = (
    "\n\n[TRUNCATED: {omitted:,} more characters of this file were not "
    "included. Only the beginning is above. If the rest matters, split the "
    "file or trim it to the relevant part.]"
)


def _record(relative: str, kind: str, text: str) -> ExtractedDoc:
    """Build a doc, enforcing the invariant that empty text always means error.

    build_corpus is the single point where every extractor's output converges,
    so the guarantee is enforced here rather than trusted from each extractor —
    and it covers text replayed from the cache too. Without it, a resource that
    yielded nothing would pass ``Corpus.readable()`` and render into the model
    context as a header with a blank body: an adjudicator reasoning about a
    document it never received, with nothing in the ingest report to say so.
    """
    if not text.strip():
        return ExtractedDoc(
            path=relative,
            kind=kind,
            text="",
            error="extractor returned no usable text",
        )
    if len(text) > MAX_DOC_CHARS:
        return ExtractedDoc(
            path=relative, kind=kind, text=_truncate(text), truncated=True
        )
    return ExtractedDoc(path=relative, kind=kind, text=text)


def _truncate(text: str) -> str:
    """Cut ``text`` to the ceiling, keeping any trailing image descriptions.

    Vision descriptions are the most expensive part of an extraction and,
    for a scanned brief, the only part that matters. They sit after the
    header in PDF and DOCX output, so the cut lands in the plain text ahead
    of them rather than on them.
    """
    head, header, tail = text.partition(EMBEDDED_IMAGES_HEADER)
    budget = max(MAX_DOC_CHARS - len(header) - len(tail), MAX_DOC_CHARS // 2)
    omitted = len(head) - budget
    if omitted <= 0:
        return text
    cut = head[:budget] + _TRUNCATION_NOTE.format(omitted=omitted)
    return cut if not header else f"{cut}\n\n{header}{tail}"


def build_corpus(
    resources_dir: Path, provider: Provider, cache: ExtractionCache
) -> Corpus:
    """Extract every discovered resource into an immutable Corpus."""
    root = Path(resources_dir)
    docs: list[ExtractedDoc] = []

    for path in discover(root):
        relative = path.relative_to(root).as_posix()
        kind, extract = extractor_for(path)
        version = cache_version(kind)

        cached = cache.get(path, version)
        if cached is not None:
            docs.append(_record(relative, kind, cached))
            continue

        try:
            text = extract(path, provider)
        except Exception as exc:  # noqa: BLE001 - never abort ingest on one file
            reason = str(exc) or type(exc).__name__
            docs.append(
                ExtractedDoc(path=relative, kind=kind, text="", error=reason)
            )
            continue

        cache.put(path, version, text)
        docs.append(_record(relative, kind, text))

    return Corpus(docs=tuple(docs))
