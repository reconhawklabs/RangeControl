import json
from datetime import datetime, timezone

from rangecontrol.advisor.models import Advice, GateResult, GateVerdict, Ruling
from rangecontrol.audit.log import AuditLog


def clock():
    return datetime(2026, 7, 28, 14, 30, 0, tzinfo=timezone.utc)


def ruling_advice():
    return Advice(
        kind="ruling",
        public_text="No. That range belongs to a partner service.",
        ruling=Ruling(
            verdict="DENIED",
            public_response="No. That range belongs to a partner service.",
            internal_reason="would break MSEL-01",
            impacted=("MSEL-01",),
            confidence="high",
        ),
        gate=GateResult(verdict=GateVerdict.CHANGE_REQUEST, reason="proposes a block"),
    )


def write(tmp_path, advice):
    log = AuditLog(tmp_path, clock=clock)
    return log, log.record(
        advice,
        user_id=42,
        user_name="blue-lead",
        channel_id=7,
        question="can we block 77.232.11.2",
        latency_ms=1234,
    )


def test_writes_one_json_line_per_record(tmp_path):
    log, _ = write(tmp_path, ruling_advice())
    write(tmp_path, ruling_advice())
    lines = log.path_for_today().read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert all(json.loads(line) for line in lines)


def test_filename_is_dated(tmp_path):
    log, _ = write(tmp_path, ruling_advice())
    assert log.path_for_today().name == "audit-20260728.jsonl"


def test_record_captures_every_field(tmp_path):
    _, record = write(tmp_path, ruling_advice())
    assert record["timestamp"] == "2026-07-28T14:30:00+00:00"
    assert record["user_id"] == 42
    assert record["user_name"] == "blue-lead"
    assert record["channel_id"] == 7
    assert record["question"] == "can we block 77.232.11.2"
    assert record["kind"] == "ruling"
    assert record["verdict"] == "DENIED"
    assert record["internal_reason"] == "would break MSEL-01"
    assert record["impacted"] == ["MSEL-01"]
    assert record["confidence"] == "high"
    assert record["gate_verdict"] == GateVerdict.CHANGE_REQUEST
    assert record["latency_ms"] == 1234


def test_deflections_are_recorded_with_null_ruling_fields(tmp_path):
    advice = Advice(
        kind="deflection",
        public_text="I only handle change requests.",
        gate=GateResult(verdict=GateVerdict.NOT_A_CHANGE_REQUEST, reason="recon"),
    )
    _, record = write(tmp_path, advice)
    assert record["kind"] == "deflection"
    assert record["verdict"] is None
    assert record["impacted"] == []


def test_errors_are_recorded(tmp_path):
    advice = Advice(kind="error", public_text="can't process", error="LLMError('x')")
    _, record = write(tmp_path, advice)
    assert record["kind"] == "error"
    assert record["error"] == "LLMError('x')"


def test_record_and_filename_dates_cannot_disagree(tmp_path):
    """One clock read per record, so a line is never filed under another day."""
    times = iter(
        [
            datetime(2026, 7, 28, 23, 59, 59, 999000, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 0, 0, 0, tzinfo=timezone.utc),
        ]
    )
    log = AuditLog(tmp_path, clock=lambda: next(times))
    record = log.record(
        Advice(kind="error", public_text="x"),
        user_id=1,
        user_name="u",
        channel_id=1,
        question="q",
        latency_ms=0,
    )
    written = [p for p in tmp_path.iterdir() if p.suffix == ".jsonl"]
    assert len(written) == 1
    assert record["timestamp"].startswith("2026-07-28")
    assert written[0].name == "audit-20260728.jsonl"


def test_records_the_public_response(tmp_path):
    _, record = write(tmp_path, ruling_advice())
    assert record["public_response"] == "No. That range belongs to a partner service."


def test_held_is_omitted_by_default(tmp_path):
    """A ruling answered immediately must not read as held in an
    after-action review."""
    _, record = write(tmp_path, ruling_advice())
    assert "held" not in record


def test_held_true_is_recorded_when_a_ruling_is_held(tmp_path):
    log = AuditLog(tmp_path, clock=clock)
    record = log.record(
        ruling_advice(), user_id=1, user_name="u", channel_id=1,
        question="q", latency_ms=0, held=True,
    )
    assert record["held"] is True


def test_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "audit"
    AuditLog(target, clock=clock).record(
        ruling_advice(),
        user_id=1,
        user_name="u",
        channel_id=1,
        question="q",
        latency_ms=0,
    )
    assert target.is_dir()


def test_unicode_survives_the_round_trip(tmp_path):
    advice = Advice(kind="deflection", public_text="—")
    log = AuditLog(tmp_path, clock=clock)
    log.record(advice, user_id=1, user_name="ü", channel_id=1, question="Ω", latency_ms=0)
    line = log.path_for_today().read_text(encoding="utf-8").strip()
    assert json.loads(line)["question"] == "Ω"


def test_write_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "audit"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    log = AuditLog(blocker, clock=clock)
    # Must not raise — a broken audit path can never take the bot down.
    log.record(
        ruling_advice(),
        user_id=1,
        user_name="u",
        channel_id=1,
        question="q",
        latency_ms=0,
    )
