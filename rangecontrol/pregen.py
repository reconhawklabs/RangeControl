"""Support for populating the cache from outside the bot.

An external coding agent extracts each resource however it likes, then hands
the text here. This module owns the cache key derivation so the external path
and the bot's own ingest can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.discovery import discover
from rangecontrol.ingest.extractors.registry import EXTRACTOR_VERSION, extractor_for


def _version_for(path: Path) -> tuple[str, str]:
    kind, _ = extractor_for(path)
    return kind, f"{EXTRACTOR_VERSION}-{kind}"


def _relative(resources_dir: Path, resource: Path) -> str:
    root = Path(resources_dir).resolve()
    target = Path(resource).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        raise ValueError(
            f"{resource} is outside the resources directory {resources_dir}"
        ) from None


def cache_put(
    resources_dir: Path, cache_dir: Path, resource: Path, text: str
) -> str:
    """Record externally-extracted text for one resource file."""
    resource = Path(resource)
    if not resource.is_file():
        raise ValueError(f"{resource} does not exist or is not a file")
    if not text.strip():
        raise ValueError(f"refusing to cache empty text for {resource}")

    relative = _relative(resources_dir, resource)
    _, version = _version_for(resource)
    ExtractionCache(cache_dir).put(resource, version, text)
    return relative


@dataclass(frozen=True)
class CacheStatus:
    total: int
    cached: tuple[str, ...]
    missing: tuple[str, ...]


def cache_status(resources_dir: Path, cache_dir: Path) -> CacheStatus:
    """Report which discovered resources already have a cache entry."""
    root = Path(resources_dir)
    cache = ExtractionCache(cache_dir)

    cached: list[str] = []
    missing: list[str] = []
    for path in discover(root):
        relative = path.relative_to(root).as_posix()
        _, version = _version_for(path)
        if cache.get(path, version) is None:
            missing.append(relative)
        else:
            cached.append(relative)

    return CacheStatus(
        total=len(cached) + len(missing),
        cached=tuple(cached),
        missing=tuple(missing),
    )


def format_status(status: CacheStatus) -> str:
    lines = [
        f"Resources discovered: {status.total}",
        f"Cached:               {len(status.cached)}",
        f"Missing:              {len(status.missing)}",
    ]
    if status.missing:
        lines.append("")
        lines.append("Not yet cached:")
        lines.extend(f"  - {path}" for path in status.missing)
    return "\n".join(lines)
