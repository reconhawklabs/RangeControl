import pytest

from rangecontrol.gui.runtime import FAILED, RUNNING, STOPPING, BotState
from rangecontrol.range_doc.report import IngestReport
from tests.support.tk import make_root

RECORD = {
    "timestamp": "2026-07-28T14:22:07+00:00",
    "user_name": "opcodeoperator",
    "channel_id": 42,
    "question": "can we block 198.51.100.7 at the edge firewall",
    "kind": "ruling",
    "verdict": "DENIED",
    "public_response": "No - vendor maintenance hold until Thursday.",
    "internal_reason": "breaks MSEL-INJECT-ALPHA C2 egress",
    "impacted": ["MSEL-INJECT-ALPHA"],
    "confidence": "high",
    "error": None,
    "latency_ms": 900,
}

REPORT = IngestReport(
    total_files=2, by_kind=(("text", 2),), unreadable=(),
    range_md_generated=False, range_md_gaps=(), index_rows=19,
    inject_count=8, context_chars=35000,
)


@pytest.fixture
def root():
    r = make_root()
    yield r
    r.destroy()


def render(root, record):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question(record)
    return tab.dump()


def test_console_renders_every_record_shape_without_crashing(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    text = render(root, RECORD)
    for fragment in ("opcodeoperator", "DENIED", "vendor maintenance hold",
                     "breaks MSEL-INJECT-ALPHA"):
        assert fragment in text

    deflection = {**RECORD, "kind": "deflection", "verdict": None,
                  "internal_reason": None, "impacted": []}
    assert "DEFLECTION" in render(root, deflection)
    assert "UNHEARD_OF_KIND" in render(root, {**RECORD, "kind": "unheard_of_kind",
                                              "verdict": None})
    assert "None" not in render(root, {"kind": "error"})

    tab = ConsoleTab(root)
    tab.append_log("white cell channel unreachable")
    assert "unreachable" in tab.dump()


def test_console_never_shows_a_held_ruling_as_delivered(root):
    held = render(root, {**RECORD, "held": True, "public_response": "Approved. Go ahead."})
    assert "HELD" in held
    assert "not sent" in held.lower()
    # Still visible to the operator, just labelled as awaiting release.
    assert "Approved. Go ahead." in held
    assert "Public:" not in held

    delivered = render(root, RECORD)
    assert "Public:" in delivered
    assert "HELD" not in delivered
    assert "Reviewer" not in delivered

    for kind in ("hitl_approved", "hitl_denied"):
        text = render(root, {**RECORD, "kind": kind, "verdict": None,
                             "reviewer": "blue-lead-reviewer"})
        assert kind.upper() in text
        assert "blue-lead-reviewer" in text


def test_status_bar_shows_the_bot_state_and_why(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_bot_state(BotState(RUNNING))
    bar.set_report(REPORT)
    assert "running" in bar.dump().lower()
    assert "19" in bar.dump()
    assert "8" in bar.dump()

    bar.set_bot_state(BotState(FAILED, "Discord rejected the bot token."))
    assert "rejected the bot token" in bar.dump()

    bar.set_bot_state(BotState(STOPPING, "the bot did not exit within the timeout"))
    text = bar.dump()
    assert "stopping" in text.lower() or "shutting down" in text.lower()
    assert "did not exit within the timeout" in text

    bar.set_bot_state(BotState("mystery", "something odd"))
    assert "mystery" in bar.dump()
    assert "something odd" in bar.dump()


def test_status_bar_pending_count_appears_and_clears(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_pending(3)
    assert "3" in bar.dump()
    bar.set_pending(0)
    assert "pending" not in bar.dump().lower()
    assert "awaiting" not in bar.dump().lower()
