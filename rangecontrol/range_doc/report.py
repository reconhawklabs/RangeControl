"""Startup ingest report and the operator confirmation gate."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from rangecontrol.ingest.corpus import Corpus
from rangecontrol.range_doc.loader import missing_sections

# Range.md plus the whole corpus goes into the system prompt of every single
# question, so context size is a recurring cost, not a one-off. Flag it well
# below any model's context window: the bill and the latency bite first.
LARGE_CONTEXT_CHARS = 400_000  # ~100k tokens

_INJECT_HEADING = re.compile(r"^###\s+\S+", re.MULTILINE)
# A data row is any pipe row that is not the header and not a separator.
# Separators must match the whole row: GFM alignment syntax (|:---:|:---:|) is
# common in generated Markdown, and counting one as data would report a
# populated dependency index when it is actually empty — suppressing the very
# warning this report exists to raise.
_SEPARATOR_ROW = re.compile(r"^\|[\s:|-]+\|\s*$")
_HEADER_ROW = re.compile(r"^\|\s*Element\s*\|")
_PIPE_ROW = re.compile(r"^\|.+\|\s*$", re.MULTILINE)


@dataclass(frozen=True)
class IngestReport:
    total_files: int
    by_kind: tuple[tuple[str, int], ...]
    unreadable: tuple[tuple[str, str], ...]
    range_md_generated: bool
    range_md_gaps: tuple[str, ...]
    index_rows: int
    inject_count: int
    context_chars: int
    # Files whose text was cut at the per-file ceiling (see discovery).
    truncated: tuple[str, ...] = ()

    def approx_context_tokens(self) -> int:
        """Rough token estimate for the per-question context (~4 chars/token)."""
        return self.context_chars // 4

    def has_concerns(self) -> bool:
        """True when the ingest produced anything an operator should weigh.

        Drives the confirmation default: a clean ingest accepts on Enter, a
        problematic one requires an explicit "y". Hitting Enter reflexively
        past "no injects were parsed" is the same looks-like-success failure
        this report exists to prevent, one layer up.
        """
        return bool(
            self.unreadable
            or self.truncated
            or self.range_md_gaps
            or self.index_rows == 0
            or self.inject_count == 0
            or self.context_chars > LARGE_CONTEXT_CHARS
        )


def _section(content: str, heading: str) -> str:
    marker = f"## {heading}"
    start = content.find(marker)
    if start == -1:
        return ""
    rest = content[start + len(marker) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _count_data_rows(section: str) -> int:
    return sum(
        1
        for row in _PIPE_ROW.findall(section)
        if not _SEPARATOR_ROW.match(row) and not _HEADER_ROW.match(row)
    )


def build_report(corpus: Corpus, range_md: str, generated: bool) -> IngestReport:
    catalog = _section(range_md, "MSEL & Inject Catalog")
    index = _section(range_md, "Protected Dependency Index")
    return IngestReport(
        context_chars=len(range_md) + len(corpus.to_prompt_text()),
        total_files=len(corpus.docs),
        by_kind=corpus.counts_by_kind(),
        unreadable=tuple((d.path, d.error or "unknown") for d in corpus.unreadable()),
        range_md_generated=generated,
        range_md_gaps=missing_sections(range_md),
        index_rows=_count_data_rows(index),
        inject_count=len(_INJECT_HEADING.findall(catalog)),
        truncated=tuple(d.path for d in corpus.truncated_docs()),
    )


def format_report(report: IngestReport) -> str:
    lines = ["", "=" * 60, "RangeControl ingest report", "=" * 60]
    lines.append(
        f"Range.md:            {'generated' if report.range_md_generated else 'loaded from disk'}"
    )
    lines.append(f"Resources found:     {report.total_files}")
    for kind, count in report.by_kind:
        lines.append(f"  {kind:<16} {count}")
    lines.append(f"Injects parsed:      {report.inject_count}")
    lines.append(f"Dependency index:    {report.index_rows} rows")
    lines.append(
        f"Ruling context:      ~{report.approx_context_tokens():,} tokens "
        f"(sent on every question)"
    )

    if report.unreadable:
        lines.append("")
        lines.append(f"Could not read {len(report.unreadable)} file(s):")
        lines.extend(f"  - {path}: {reason}" for path, reason in report.unreadable)

    if report.truncated:
        lines.append("")
        lines.append(
            f"WARNING — {len(report.truncated)} file(s) were too large and only "
            "their beginning was used:"
        )
        lines.extend(f"  - {path}" for path in report.truncated)

    if report.range_md_gaps:
        lines.append("")
        lines.append("WARNING — Range.md is missing sections:")
        lines.extend(f"  - {name}" for name in report.range_md_gaps)

    if report.index_rows == 0:
        lines.append("")
        lines.append(
            "WARNING — the Protected Dependency Index is empty. Rulings will be "
            "guesswork until it is populated."
        )
    if report.context_chars > LARGE_CONTEXT_CHARS:
        lines.append("")
        lines.append(
            f"WARNING — the ruling context is ~{report.approx_context_tokens():,} "
            "tokens and is re-sent with every question. Expect noticeable cost "
            "and latency per ruling, and a hard failure if it approaches the "
            "model's context window. Consider trimming resources/."
        )

    if report.inject_count == 0:
        lines.append("")
        lines.append(
            "WARNING — no injects were parsed. Check that MSEL material reached "
            "resources/ and survived extraction."
        )

    lines.append("=" * 60)
    return "\n".join(lines)


def confirm(
    report: IngestReport,
    auto_yes: bool,
    out: TextIO | None = None,
    read_line: Callable[[], str] | None = None,
) -> bool:
    """Print the report and ask the operator to proceed. --yes skips the wait."""
    out = sys.stdout if out is None else out
    read_line = input if read_line is None else read_line

    print(format_report(report), file=out)
    if auto_yes:
        print("Proceeding (--yes).", file=out)
        return True

    print("", file=out)
    if report.has_concerns():
        print("Start RangeControl with this range? [y/N] ", end="", file=out)
        out.flush()
        return read_line().strip().lower() in {"y", "yes"}

    print("Start RangeControl with this range? [Y/n] ", end="", file=out)
    out.flush()
    return read_line().strip().lower() not in {"n", "no"}
