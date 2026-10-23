import json
from dataclasses import replace
from datetime import datetime, timezone

from rangecontrol.advisor.models import Advice, GateResult, GateVerdict, Ruling
from rangecontrol.audit.log import AuditLog

DRAFT = "No. That is waiting on the change board."


def clock():
    return datetime(2026, 7, 28, 14, 30, 0, tzinfo=timezone.utc)


def ruling_advice():
    return Advice(
        kind="ruling",
        public_text=DRAFT,
        ruling=Ruling(
            verdict="DENIED",
            public_response=DRAFT,
            internal_reason="would break MSEL-01",
            impacted=("MSEL-01",),
            confidence="high",
        ),
        gate=GateResult(verdict=GateVerdict.CHANGE_REQUEST, reason="proposes a block"),
    )


def record(log, advice, **kwargs):
    fields = {
        "user_id": 42,
        "user_name": "blue-lead",
        "channel_id": 7,
        "question": "can we block 77.232.11.2",
        "latency_ms": 1234,
        **kwargs,
    }
    log.record(advice, **fields)


def lines_of(log):
    text = log.path_for_today().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_each_ruling_is_one_dated_json_line_with_every_field(tmp_path):
    """Written to a directory created on demand; held/screen fields are
    omitted when they do not apply, so nothing reads as held or screened."""
    log = AuditLog(tmp_path / "nested" / "audit", clock=clock)
    record(log, ruling_advice())
    record(log, ruling_advice())

    assert log.path_for_today() == tmp_path / "nested" / "audit" / "audit-20260728.jsonl"
    first, second = lines_of(log)
    assert first == second
    assert first["timestamp"] == "2026-07-28T14:30:00+00:00"
    assert first["user_id"] == 42
    assert first["user_name"] == "blue-lead"
    assert first["channel_id"] == 7
    assert first["question"] == "can we block 77.232.11.2"
    assert first["kind"] == "ruling"
    assert first["verdict"] == "DENIED"
    assert first["public_response"] == DRAFT
    assert first["internal_reason"] == "would break MSEL-01"
    assert first["impacted"] == ["MSEL-01"]
    assert first["confidence"] == "high"
    assert first["gate_verdict"] == GateVerdict.CHANGE_REQUEST
    assert first["latency_ms"] == 1234
    for absent in ("held", "screen_reason", "model_draft"):
        assert absent not in first


def test_deflections_and_errors_are_recorded(tmp_path):
    log = AuditLog(tmp_path, clock=clock)
    deflection = Advice(
        kind="deflection",
        public_text="I only handle change requests.",
        gate=GateResult(verdict=GateVerdict.NOT_A_CHANGE_REQUEST, reason="recon"),
    )
    record(log, deflection, user_name="ü", question="Ω")
    record(log, Advice(kind="error", public_text="can't process", error="LLMError('x')"))

    deflected, errored = lines_of(log)
    assert deflected["kind"] == "deflection"
    assert deflected["verdict"] is None
    assert deflected["impacted"] == []
    assert (deflected["user_name"], deflected["question"]) == ("ü", "Ω")
    assert errored["kind"] == "error"
    assert errored["error"] == "LLMError('x')"


def test_a_held_screened_ruling_records_what_was_withheld(tmp_path):
    """The after-action review must tell the delivered text from the model's
    withheld draft, and a held ruling from one that went out."""
    screened = replace(
        ruling_advice(),
        public_text="Not approved. It needs a sign-off that hasn't been given.",
        screen_reason="describes ownership",
    )
    log = AuditLog(tmp_path, clock=clock)
    record(log, screened, held=True)

    [line] = lines_of(log)
    assert line["held"] is True
    assert line["public_response"] == screened.public_text
    assert line["model_draft"] == DRAFT
    assert line["screen_reason"] == "describes ownership"


def test_record_and_filename_dates_cannot_disagree(tmp_path):
    """One clock read per record, so a line is never filed under another day."""
    times = iter(
        [
            datetime(2026, 7, 28, 23, 59, 59, 999000, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 0, 0, 0, tzinfo=timezone.utc),
        ]
    )
    log = AuditLog(tmp_path, clock=lambda: next(times))
    written_record = log.record(
        Advice(kind="error", public_text="x"),
        user_id=1,
        user_name="u",
        channel_id=1,
        question="q",
        latency_ms=0,
    )
    written = [p for p in tmp_path.iterdir() if p.suffix == ".jsonl"]
    assert len(written) == 1
    assert written_record["timestamp"].startswith("2026-07-28")
    assert written[0].name == "audit-20260728.jsonl"


def test_write_failure_does_not_raise(tmp_path):
    """A broken audit path can never take the bot down."""
    blocker = tmp_path / "audit"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    record(AuditLog(blocker, clock=clock), ruling_advice())
