import os
import sys
from pathlib import Path

import pytest

from rangecontrol.gui.paths import (
    PathError,
    app_home,
    bundle_root,
    ensure_app_home,
    is_frozen,
)


def test_not_frozen_under_pytest():
    assert is_frozen() is False


def test_app_home_is_cwd_when_not_frozen(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert app_home() == tmp_path.resolve()


def test_app_home_is_the_executables_folder_when_frozen(monkeypatch, tmp_path):
    """A double-click and a terminal launch must land in the same place."""
    exe = tmp_path / "bin" / "RangeControl"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert app_home() == exe.parent.resolve()


def test_bundle_root_is_the_package_when_not_frozen():
    assert (bundle_root() / "RangeTemplate.md").is_file()


def test_bundle_root_is_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert bundle_root() == tmp_path.resolve()


def test_ensure_app_home_accepts_a_writable_directory(tmp_path):
    ensure_app_home(tmp_path)  # must not raise


def test_ensure_app_home_rejects_an_unwritable_directory(tmp_path):
    """A binary parked in Program Files must say so at startup, not later."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(PathError, match=str(locked)):
            ensure_app_home(locked)
    finally:
        locked.chmod(0o700)


def test_ensure_app_home_creates_a_missing_directory(tmp_path):
    target = tmp_path / "new"
    ensure_app_home(target)
    assert target.is_dir()


def test_ensure_app_home_rejects_a_file(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x")
    with pytest.raises(PathError):
        ensure_app_home(target)
