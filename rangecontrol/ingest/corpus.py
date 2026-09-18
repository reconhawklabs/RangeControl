"""Immutable representation of extracted range resources."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedDoc:
    """One resource file reduced to text.

    ``error`` is non-None when extraction failed; ``text`` is then empty and
    the document is excluded from the model context.
    """

    path: str
    kind: str
    text: str
    error: str | None = None
    # True when ``text`` is the head of a file too large to include whole.
    # The cut is also marked inside ``text`` so the model knows it is
    # reading a prefix, not the document.
    truncated: bool = False


@dataclass(frozen=True)
class Corpus:
    """The full set of extracted resources."""

    docs: tuple[ExtractedDoc, ...]

    def readable(self) -> tuple[ExtractedDoc, ...]:
        return tuple(d for d in self.docs if d.error is None)

    def unreadable(self) -> tuple[ExtractedDoc, ...]:
        return tuple(d for d in self.docs if d.error is not None)

    def truncated_docs(self) -> tuple[ExtractedDoc, ...]:
        return tuple(d for d in self.docs if d.error is None and d.truncated)

    def counts_by_kind(self) -> tuple[tuple[str, int], ...]:
        counts = Counter(d.kind for d in self.readable())
        return tuple(sorted(counts.items()))

    def with_doc(self, doc: ExtractedDoc) -> Corpus:
        """Return a new Corpus with ``doc`` appended."""
        return Corpus(docs=self.docs + (doc,))

    def to_prompt_text(self) -> str:
        """Render readable documents as a delimited block for the model context."""
        sections = [
            f"===== RESOURCE: {d.path} =====\n{d.text}".rstrip() for d in self.readable()
        ]
        return "\n\n".join(sections)
