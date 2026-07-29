"""Tk root fixture that skips rather than fails where there is no display."""

from __future__ import annotations

import pytest


def make_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no DISPLAY, or Tk not built in
        pytest.skip(f"no usable display: {exc}")
    root.withdraw()
    return root
