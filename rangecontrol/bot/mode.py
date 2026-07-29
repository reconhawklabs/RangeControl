"""A boolean the GUI can flip while the bot is running.

The checkbox lives on Tk's main thread; the bot reads this from its own. An
Event rather than a bare attribute so the change is unambiguously published
across threads, and so a future waiter can block on it without a poll loop.
"""

from __future__ import annotations

import threading


class ModeSwitch:
    def __init__(self, enabled: bool = False) -> None:
        self._event = threading.Event()
        self.set(enabled)

    @property
    def enabled(self) -> bool:
        return self._event.is_set()

    def set(self, value: bool) -> None:
        if value:
            self._event.set()
        else:
            self._event.clear()
