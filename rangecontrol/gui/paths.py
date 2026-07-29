"""Where RangeControl reads its bundle from and writes the user's range to.

These are two different directories in a frozen binary and confusing them is
the classic one-file packaging bug: user data written under the bundle root
lives in a temporary directory that is deleted when the process exits.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


class PathError(Exception):
    """The app home cannot be used. Reported at startup, never swallowed."""


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def app_home() -> Path:
    """The folder holding the user's range: .env, resources/, Range.md.

    When frozen this is the directory containing the executable, not the
    working directory. A desktop launcher hands the process $HOME, so using
    the cwd would silently build a range somewhere the user never chose.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def bundle_root() -> Path:
    """Read-only root holding packaged data files.

    Under a one-file build this is the temporary extraction directory, which
    is removed when the process exits. Nothing user-owned may be written here.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
    return Path(__file__).resolve().parent.parent


def ensure_app_home(home: Path) -> None:
    """Create ``home`` if needed and prove it is writable.

    Probing with a real file rather than os.access: access() consults
    permission bits, which lie under read-only mounts, ACLs, and on Windows.
    A binary dropped in Program Files must fail here with a clear message
    rather than midway through a regenerate.
    """
    home = Path(home)
    if home.exists() and not home.is_dir():
        raise PathError(f"{home} exists but is not a directory.")

    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home, prefix=".rc-write-test"):
            pass
    except OSError as exc:
        raise PathError(
            f"Cannot write to {home}: {exc.strerror or exc}. "
            "Move RangeControl to a folder you own, such as your home "
            "directory or Desktop, and start it again."
        ) from exc
