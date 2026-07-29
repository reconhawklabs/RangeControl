"""Content-addressed extraction cache.

Keyed by file content hash plus extractor version, so restarting mid-exercise
costs seconds instead of a full re-ingest, and editing a resource or bumping an
extractor invalidates exactly the affected entries.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path


class ExtractionCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key(self, path: Path, version: str) -> Path:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._dir / f"{version}-{digest}.txt"

    def get(self, path: Path, version: str) -> str | None:
        entry = self._key(path, version)
        if not entry.exists():
            return None
        try:
            return entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def put(self, path: Path, version: str, text: str) -> None:
        """Write an entry atomically.

        A plain write truncates in place, so a crash mid-write would leave a
        readable but truncated file that ``get`` returns as a valid hit — and a
        truncated extraction reaches the adjudicating model looking complete.
        Write to a temp file in the same directory, then rename.
        """
        entry = self._key(path, version)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._dir, delete=False, suffix=".tmp"
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(text)
            os.replace(temp_path, entry)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        """Remove every cached entry.

        ``ignore_errors=True`` used to swallow a partial failure here — the
        exact same "looks clean but isn't" shape this project keeps closing
        elsewhere, except this time on the operator's own recovery action: a
        partial failure would leave stale entries in place while the ingest
        report announces a fresh regenerate. Raise instead, naming the
        directory, so a failed --regenerate is visibly a failure.
        """
        if self._dir.exists():
            try:
                shutil.rmtree(self._dir)
            except OSError as exc:
                raise OSError(
                    f"failed to clear extraction cache directory {self._dir}: {exc}"
                ) from exc
        self._dir.mkdir(parents=True, exist_ok=True)
