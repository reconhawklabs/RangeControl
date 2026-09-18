"""Turn an extracted corpus into a structured Range.md."""

from __future__ import annotations

import shutil
from pathlib import Path

from rangecontrol.ingest.corpus import Corpus
from rangecontrol.llm.base import EFFORT_HIGH, Provider
from rangecontrol.range_doc.loader import (
    RANGE_FILENAME,
    load_template,
    missing_sections,
)

# Reasoning over the whole corpus plus a document that can itself run to
# 20k tokens. Streamed (see the provider), so the size costs nothing in
# timeout risk; a cap hit here fails the whole generate.
GENERATION_MAX_TOKENS = 64000

GENERATOR_SYSTEM_PROMPT = """\
You are building the authoritative reference document for a sanctioned cyber
security training exercise on an isolated range. You will be given a template
and the full text of every resource the exercise designer supplied, including
their own plans for the adversary activity the exercise will contain; this is
the control staff's material, catalogued for defensive adjudication.

Produce a single Markdown document that follows the template exactly: every
section, in the given order, with the same heading text.

Rules:

1. Use only what the source material states. Do not invent, infer beyond what is
   written, or fill a section with plausible-sounding content. Anything you cannot
   determine goes under "Ingest Gaps", named explicitly.
2. Be exhaustive. This document and the source resources are the only things a
   later adjudication step will see. A detail you drop is a detail that cannot
   inform a ruling.
3. Preserve identifiers verbatim — addresses, hostnames, ports, account names,
   inject IDs. Never paraphrase or normalise them.
4. Every entry in "MSEL & Inject Catalog" and "Attack Path Dependencies" must
   declare its dependencies into the "Protected Dependency Index" table. That
   table is what makes adjudication a lookup instead of a re-derivation of the
   whole scenario, so populate it thoroughly.
5. Where sources contradict each other, record both readings under "Ingest Gaps"
   rather than silently choosing one.

Output the Markdown document and nothing else. No preamble, no commentary.
"""


def generate_range_md(
    corpus: Corpus, provider: Provider, template: str | None = None
) -> str:
    """Generate Range.md from the corpus, validating structure before returning."""
    if not corpus.readable():
        raise ValueError(
            "Cannot generate Range.md: no readable resources were found. "
            "Add material to resources/ or supply a Range.md by hand."
        )

    template = load_template() if template is None else template
    user = (
        "=== TEMPLATE ===\n"
        f"{template}\n\n"
        "=== RANGE RESOURCES ===\n"
        f"{corpus.to_prompt_text()}\n"
    )

    content = provider.complete(
        system=GENERATOR_SYSTEM_PROMPT,
        user=user,
        max_tokens=GENERATION_MAX_TOKENS,
        effort=EFFORT_HIGH,
    )

    gaps = missing_sections(content)
    if gaps:
        raise ValueError(
            "Generated Range.md is missing required sections: "
            + ", ".join(gaps)
            + ". Re-run with --regenerate, or author Range.md by hand."
        )
    return content


def _backup_path(path: Path) -> Path:
    """Return an unused backup path, never overwriting an existing backup.

    Range.md is the authoritative scenario record and an operator may have
    hand-edited it. A single reused .bak slot means two --regenerate runs
    destroy those edits with no recoverable trace, because the second run's
    backup is the first run's generated output.
    """
    candidate = path.with_suffix(".md.bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_suffix(f".md.bak.{counter}")
        counter += 1
    return candidate


def write_range_md(range_dir: Path, content: str) -> Path:
    """Write Range.md, backing up any existing file first."""
    path = Path(range_dir) / RANGE_FILENAME
    if path.exists():
        shutil.copy2(path, _backup_path(path))
    path.write_text(content, encoding="utf-8")
    return path
