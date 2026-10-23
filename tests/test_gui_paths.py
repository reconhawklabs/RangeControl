import os
import sys

import pytest

from rangecontrol.gui.paths import (
    PathError,
    app_home,
    bundle_root,
    ensure_app_home,
    is_frozen,
)


def test_a_source_checkout_uses_the_cwd_and_the_package(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert is_frozen() is False
    assert app_home() == tmp_path.resolve()
    assert (bundle_root() / "RangeTemplate.md").is_file()


def test_a_frozen_build_uses_the_exe_folder_and_meipass(monkeypatch, tmp_path):
    """User data under the bundle root would vanish with the temp extraction
    dir, and a double-click and a terminal launch must land in the same place."""
    exe = tmp_path / "bin" / "RangeControl"
    exe.parent.mkdir()
    exe.write_text("")
    meipass = tmp_path / "_MEI1234"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    assert is_frozen() is True
    assert app_home() == exe.parent.resolve()
    assert bundle_root() == meipass.resolve()


def test_ensure_app_home_creates_or_rejects_with_a_clear_error(tmp_path):
    """A binary parked in Program Files must say so at startup, not later."""
    missing = tmp_path / "new"
    ensure_app_home(missing)
    assert missing.is_dir()
    ensure_app_home(missing)  # an existing writable directory is accepted

    a_file = tmp_path / "afile"
    a_file.write_text("x")
    with pytest.raises(PathError):
        ensure_app_home(a_file)

    if os.geteuid() == 0:
        return  # root ignores permission bits
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(PathError, match=str(locked)):
            ensure_app_home(locked)
    finally:
        locked.chmod(0o700)
