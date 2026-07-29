"""The single channel from background threads to the Tk main loop.

Every worker publishes Events here; the GUI drains the queue on a timer. This
is the only sanctioned way to move data toward a widget — touching Tk from a
worker thread corrupts the interpreter in ways that surface much later as
unrelated crashes.
"""

from __future__ import annotations

import logging
import queue
from dataclasses import dataclass

from rangecontrol.audit.log import AuditLog

QUESTION = "question"  # payload: the audit record dict
STATUS = "status"      # payload: BotState
INGEST = "ingest"      # payload: IngestOutcome
ERROR = "error"        # payload: str
LOG = "log"
MODELS = "models"     # payload: ModelsOutcome            # payload: str
PENDING = "pending"    # payload: int (current count of held HITL requests)


@dataclass(frozen=True)
class Event:
    kind: str
    payload: object


def publish(events: "queue.Queue[Event]", kind: str, payload: object) -> None:
    """Publish without ever blocking or raising.

    A stalled or full queue means the window is busy or gone. Neither is a
    reason to interrupt an exercise, so the event is dropped: this channel
    carries a convenience view, while the authoritative record is the JSONL
    audit trail written independently.
    """
    try:
        events.put_nowait(Event(kind=kind, payload=payload))
    except Exception:  # noqa: BLE001 - queue problems must not break adjudication
        pass


class QueueAuditLog(AuditLog):
    """AuditLog that also publishes each record to the GUI.

    Subclassing rather than modifying the bot: handle_question already calls
    record() and uses its return value, so the live feed costs no change to
    any adjudication code path.
    """

    def __init__(self, audit_dir, events, clock=None) -> None:
        super().__init__(audit_dir, clock)
        self._events = events

    def record(self, advice, **kwargs) -> dict:
        record = super().record(advice, **kwargs)
        publish(self._events, QUESTION, record)
        return record


class QueueLogHandler(logging.Handler):
    """Routes RangeControl's own warnings into the console feed.

    Without this they go to a terminal that a packaged user does not have.
    The white-cell-unreachable warning in particular must reach a human.

    Errors during formatting or publishing are silently swallowed (not reported
    via handleError) because a packaged GUI user has no terminal to see them.
    This trades visibility during development for robustness: a formatting bug
    in this handler will leave no trace without explicit debugging.
    """

    def __init__(self, events) -> None:
        super().__init__()
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            publish(self._events, LOG, self.format(record))
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            pass
