import logging
import queue

from rangecontrol.advisor.models import Advice, Ruling
from rangecontrol.gui.events import LOG, QUESTION, Event, QueueAuditLog, QueueLogHandler

RULING = Ruling(
    verdict="DENIED",
    public_response="No.",
    internal_reason="breaks MSEL-INJECT-ALPHA",
    impacted=("MSEL-INJECT-ALPHA",),
    confidence="high",
)


def advice():
    return Advice(kind="ruling", public_text="No.", ruling=RULING)


def record(log):
    return log.record(advice(), user_id=1, user_name="u", channel_id=2,
                      question="q", latency_ms=5)


class _HostileQueue:
    """A queue whose put_nowait raises an exception other than queue.Full."""

    def __init__(self, exc):
        self._exc = exc

    def put_nowait(self, item):
        raise self._exc


def test_an_audit_record_is_written_returned_and_published(tmp_path):
    """bot/client.py relies on the return value; the Console relies on the event."""
    events = queue.Queue()
    result = record(QueueAuditLog(tmp_path, events))

    assert list(tmp_path.glob("audit-*.jsonl"))
    assert result["verdict"] == "DENIED"
    event = events.get_nowait()
    assert event.kind == QUESTION
    assert event.payload["internal_reason"] == "breaks MSEL-INJECT-ALPHA"
    assert event.payload["question"] == "q"


def test_log_handler_publishes_warnings_to_the_console_feed():
    events = queue.Queue()
    handler = QueueLogHandler(events)
    logger = logging.getLogger("rangecontrol.test")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        logger.warning("white cell channel unreachable")
    finally:
        logger.removeHandler(handler)
    event = events.get_nowait()
    assert event.kind == LOG
    assert "unreachable" in event.payload


def test_a_full_or_broken_queue_never_breaks_adjudication_or_logging(tmp_path):
    """The feed is a convenience. It must not be able to take the bot down."""
    full = queue.Queue(maxsize=1)
    full.put(Event(kind="filler", payload=None))
    feeds = {
        "full": full,
        "runtime-error": _HostileQueue(RuntimeError("window is gone")),
        "memory-error": _HostileQueue(MemoryError("boom")),
    }
    for name, events in feeds.items():
        audit_dir = tmp_path / name
        audit_dir.mkdir()
        result = record(QueueAuditLog(audit_dir, events))
        assert result["verdict"] == "DENIED", name
        assert list(audit_dir.glob("audit-*.jsonl")), name

        handler = QueueLogHandler(events)
        handler.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "msg", None, None))
