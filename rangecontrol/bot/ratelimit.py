"""Per-user sliding-window rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        max_calls: int = 5,
        per_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_calls = max_calls
        self._window = per_seconds
        self._clock = clock
        self._calls: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        """Record and permit a call, or return False when over the limit."""
        now = self._clock()
        history = self._calls[user_id]

        while history and now - history[0] >= self._window:
            history.popleft()

        if len(history) >= self._max_calls:
            return False

        history.append(now)
        return True
