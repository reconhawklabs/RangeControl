"""Rulings held for white cell approval, keyed by their approval message.

Deliberately in memory only. A restart drops every pending request, and that
is the correct failure: releasing a ruling nobody approved because it happened
to survive a restart would defeat the entire point of the mode. An operator
whose bot restarted re-asks the question.

Holds no lock: safe only because both ``add`` and ``take`` are called from the
bot's own asyncio event loop (posting the white cell message and reacting to
it both happen there). Never call it from the Tk thread.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Generous for any real exercise. The cap exists so an unattended white cell
# cannot grow the process without bound, not to ration reviews.
MAX_PENDING = 500


@dataclass(frozen=True)
class PendingRequest:
    channel_id: int
    message_id: int
    user_id: int
    user_name: str
    question: str
    public_text: str


class PendingRegistry:
    def __init__(self, on_change: Callable[[int], None] | None = None) -> None:
        self._items: OrderedDict[int, PendingRequest] = OrderedDict()
        # Lets the GUI keep a live "N awaiting review" count without polling
        # __len__ from another thread: add()/take() both run on the bot's
        # own asyncio loop (see the module docstring), so calling this here
        # is exactly as safe as the mutation it follows, and no less so.
        self._on_change = on_change

    def add(self, approval_message_id: int, request: PendingRequest) -> None:
        self._items[approval_message_id] = request
        while len(self._items) > MAX_PENDING:
            dropped_id, dropped = self._items.popitem(last=False)
            logger.warning(
                "dropped an unreviewed request from %s (approval message %s); "
                "the blue team was never answered",
                dropped.user_name,
                dropped_id,
            )
        self._notify()

    def take(self, approval_message_id: int) -> PendingRequest | None:
        """Return and remove a request, or None if it is not held.

        Removal is the point: two reviewers reacting, or one reacting twice,
        must not post the reply twice.
        """
        request = self._items.pop(approval_message_id, None)
        if request is not None:
            self._notify()
        return request

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(len(self._items))

    def __len__(self) -> int:
        return len(self._items)
