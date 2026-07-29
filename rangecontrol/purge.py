"""Startup offer to discard a previous run's state and rebuild from resources/.

Between exercises the resources change but Range.md and the extraction cache do
not: both are keyed to what was ingested last time. An operator who drops new
material into resources/ and restarts would otherwise keep adjudicating against
the previous exercise's document, because Range.md exists so nothing regenerates
it. This asks, but only when there is something to ask about.

Choosing to regenerate is exactly ``--regenerate``: the cache is cleared and
Range.md is rebuilt, with the old one copied to a backup first.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from rangecontrol.range_doc.loader import RANGE_FILENAME, load_range_md


@dataclass(frozen=True)
class ExistingState:
    """What a previous run left behind in the range directory."""

    range_md: Path | None
    cache_entries: int

    def exists(self) -> bool:
        return self.range_md is not None or self.cache_entries > 0


def inspect_existing(range_dir: Path, cache_dir: Path) -> ExistingState:
    """Report the reusable state already present, without touching any of it."""
    range_path = Path(range_dir) / RANGE_FILENAME
    # load_range_md's own definition of present: a blank Range.md is treated as
    # absent everywhere else, so it must not show up here as something to purge.
    present = load_range_md(range_dir) is not None

    cache = Path(cache_dir)
    entries = 0
    if cache.is_dir():
        # Only committed entries. A leftover .tmp is a crashed write, not a
        # cached resource, and counting it would overstate what is being lost.
        entries = sum(1 for entry in cache.glob("*.txt") if entry.is_file())

    return ExistingState(
        # Resolved: the range directory is often ".", and a bare relative
        # "Range.md" next to the label reads as a repeated word rather than a
        # location the operator can go look at before answering.
        range_md=range_path.resolve() if present else None,
        cache_entries=entries,
    )


def _wrap(text: str) -> str:
    """Fit prose to a narrow terminal; the prompt is read under time pressure."""
    return textwrap.fill(text, width=76)


def prompt_purge(
    state: ExistingState,
    out: TextIO | None = None,
    read_line: Callable[[], str] | None = None,
) -> bool:
    """Ask whether to discard ``state`` and regenerate. Defaults to keeping it.

    Keeping is the default because regenerating re-reads every resource through
    the provider: it costs API calls, takes time, and produces a new Range.md
    that the operator has not reviewed yet. Nothing here is urgent enough to be
    the answer someone gives by reflex.
    """
    out = sys.stdout if out is None else out
    read_line = input if read_line is None else read_line

    print("", file=out)
    print("Existing range state found:", file=out)
    if state.range_md is not None:
        print(f"  {'Range doc':<11} {state.range_md}", file=out)
    if state.cache_entries:
        plural = "" if state.cache_entries == 1 else "s"
        print(
            f"  {'Cache':<11} {state.cache_entries} "
            f"extracted resource{plural}",
            file=out,
        )
    print("", file=out)
    print(
        _wrap(
            "Regenerating re-reads every resource through the AI, which costs "
            "API calls and time."
        ),
        file=out,
    )
    # Only describe what is actually on the line. Naming a consequence for a
    # file that is not there reads as a warning about something at risk.
    consequences = []
    if state.range_md is not None:
        consequences.append(f"the current {RANGE_FILENAME} is backed up first")
    if state.cache_entries:
        consequences.append("cached extractions are deleted")
    if consequences:
        print(_wrap(f"If you continue, {' and '.join(consequences)}."), file=out)
    print("", file=out)
    print("Discard this and regenerate from resources/? [y/N] ", end="", file=out)
    out.flush()

    try:
        answer = read_line()
    except EOFError:
        # A closed stdin is not consent to throw away the operator's state.
        print("", file=out)
        return False
    return answer.strip().lower() in {"y", "yes"}
