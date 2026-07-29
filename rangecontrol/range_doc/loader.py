"""Reading Range.md and the template that defines its structure."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

RANGE_FILENAME = "Range.md"
TEMPLATE_FILENAME = "RangeTemplate.md"

REQUIRED_SECTIONS = (
    "Exercise Overview",
    "Network Topology",
    "Asset Inventory",
    "Required Access Paths",
    "Firewall & Policy Baseline",
    "Users & Accounts",
    "Automation & Scripts",
    "MSEL & Inject Catalog",
    "Attack Path Dependencies",
    "Scoring & Availability Requirements",
    "Intentional Vulnerabilities",
    "Out of Scope / Do Not Touch",
    "Standing Adjudication Rules",
    "Protected Dependency Index",
    "Ingest Gaps",
)


def load_range_md(range_dir: Path) -> str | None:
    """Return the contents of Range.md, or None when absent or empty."""
    path = Path(range_dir) / RANGE_FILENAME
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    return content if content.strip() else None


def load_template() -> str:
    """Return RangeTemplate.md, which ships as package data.

    Read through importlib.resources rather than a path relative to __file__:
    the same call has to work from a source checkout, an installed wheel, and
    a PyInstaller one-file bundle, where __file__ points into a temporary
    extraction directory that has no repository above it.
    """
    try:
        return (
            resources.files("rangecontrol")
            .joinpath(TEMPLATE_FILENAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise FileNotFoundError(
            f"{TEMPLATE_FILENAME} is missing from the rangecontrol package. "
            "The installation or binary is incomplete."
        ) from exc


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<name>.+?)\s*$", re.MULTILINE)


def _headings(content: str) -> set[str]:
    """Extract heading text from markdown content."""
    return {match.group("name").strip() for match in _HEADING.finditer(content)}


def missing_sections(content: str) -> tuple[str, ...]:
    """Return required sections that do not appear as a heading in ``content``.

    Matching is anchored to headings rather than plain substrings. The
    generator is instructed to name anything it could not determine under
    "Ingest Gaps", so prose such as "Automation & Scripts could not be
    determined" would otherwise make a genuinely absent section look present
    and let a structurally incomplete document pass Task 13's accept gate.
    """
    present = _headings(content)
    return tuple(name for name in REQUIRED_SECTIONS if name not in present)
