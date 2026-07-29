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


def test_audit_log_still_writes_its_jsonl(tmp_path):
    events = queue.Queue()
    log = QueueAuditLog(tmp_path, events)
    log.record(advice(), user_id=1, user_name="u", channel_id=2,
               question="q", latency_ms=5)
    assert list(tmp_path.glob("audit-*.jsonl"))


def test_the_record_is_published_to_the_queue(tmp_path):
    events = queue.Queue()
    log = QueueAuditLog(tmp_path, events)
    log.record(advice(), user_id=1, user_name="u", channel_id=2,
               question="q", latency_ms=5)
    event = events.get_nowait()
    assert event.kind == QUESTION
    assert event.payload["internal_reason"] == "breaks MSEL-INJECT-ALPHA"
    assert event.payload["question"] == "q"


def test_record_still_returns_the_dict(tmp_path):
    """Callers in bot/client.py rely on the return value."""
    events = queue.Queue()
    log = QueueAuditLog(tmp_path, events)
    result = log.record(advice(), user_id=1, user_name="u", channel_id=2,
                        question="q", latency_ms=5)
    assert result["verdict"] == "DENIED"


def test_a_full_queue_never_breaks_adjudication(tmp_path):
    """The feed is a convenience. It must not be able to take the bot down."""
    events = queue.Queue(maxsize=1)
    events.put(Event(kind="filler", payload=None))
    log = QueueAuditLog(tmp_path, events)
    result = log.record(advice(), user_id=1, user_name="u", channel_id=2,
                        question="q", latency_ms=5)
    assert result["verdict"] == "DENIED"
    assert list(tmp_path.glob("audit-*.jsonl"))


def test_log_handler_publishes_warnings():
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


def test_log_handler_survives_a_full_queue():
    events = queue.Queue(maxsize=1)
    events.put(Event(kind="filler", payload=None))
    handler = QueueLogHandler(events)
    handler.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "msg", None, None))


class _HostileQueue:
    """A queue whose put_nowait raises an exception other than queue.Full."""

    def __init__(self, exc):
        self._exc = exc

    def put_nowait(self, item):
        raise self._exc


def test_record_returns_dict_even_if_queue_raises_runtime_error(tmp_path):
    """A hostile queue should not break adjudication."""
    events = _HostileQueue(RuntimeError("window is gone"))
    log = QueueAuditLog(tmp_path, events)
    result = log.record(advice(), user_id=1, user_name="u", channel_id=2,
                        question="q", latency_ms=5)
    assert result["verdict"] == "DENIED"
    assert list(tmp_path.glob("audit-*.jsonl"))


def test_record_returns_dict_even_if_queue_raises_memory_error(tmp_path):
    """A hostile queue should not break adjudication."""
    events = _HostileQueue(MemoryError("boom"))
    log = QueueAuditLog(tmp_path, events)
    result = log.record(advice(), user_id=1, user_name="u", channel_id=2,
                        question="q", latency_ms=5)
    assert result["verdict"] == "DENIED"
    assert list(tmp_path.glob("audit-*.jsonl"))


def test_log_handler_survives_queue_raising_runtime_error():
    """Handler errors must not escape."""
    events = _HostileQueue(RuntimeError("window is gone"))
    handler = QueueLogHandler(events)
    handler.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "msg", None, None))


def test_log_handler_survives_queue_raising_memory_error():
    """Handler errors must not escape."""
    events = _HostileQueue(MemoryError("boom"))
    handler = QueueLogHandler(events)
    handler.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "msg", None, None))
