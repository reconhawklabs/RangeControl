"""Append-only JSONL audit log.

Every interaction is recorded, including gate deflections and errors. A failure
to write is logged and swallowed — losing an audit line must never take the bot
offline mid-exercise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from rangecontrol.advisor.models import Advice

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog:
    def __init__(
        self, audit_dir: Path, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._dir = Path(audit_dir)
        self._clock = clock or _utcnow

    def path_for_today(self) -> Path:
        return self._path_for(self._clock())

    def _path_for(self, moment: datetime) -> Path:
        return self._dir / f"audit-{moment.strftime('%Y%m%d')}.jsonl"

    def record(
        self,
        advice: Advice,
        *,
        user_id: int,
        user_name: str,
        channel_id: int,
        question: str,
        latency_ms: int,
        reviewer: str = "",
        held: bool = False,
    ) -> dict:
        # Read the clock once. Two reads could straddle UTC midnight and file a
        # line under a different day than its own timestamp records — a gap in
        # the trail exactly where someone reconstructing a timeline would look.
        now = self._clock()

        ruling = advice.ruling
        record = {
            "timestamp": now.isoformat(),
            "user_id": user_id,
            "user_name": user_name,
            "channel_id": channel_id,
            "question": question,
            "kind": advice.kind,
            "gate_verdict": advice.gate.verdict if advice.gate else None,
            "verdict": ruling.verdict if ruling else None,
            "public_response": advice.public_text,
            "internal_reason": ruling.internal_reason if ruling else None,
            "impacted": list(ruling.impacted) if ruling else [],
            "confidence": ruling.confidence if ruling else None,
            "error": advice.error,
            "latency_ms": latency_ms,
        }
        if reviewer:
            record["reviewer"] = reviewer
        if held:
            # A ruling held for white cell approval has ``public_response``
            # set to the text that is *awaiting* release, not what the blue
            # team actually received (HELD_TEXT) -- without this flag an
            # after-action review reads a delivered ruling that was, in
            # fact, withheld. Omitted rather than always written as False:
            # every existing consumer of this record predates the field.
            record["held"] = True

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._path_for(now).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("failed to write audit record")

        return record
