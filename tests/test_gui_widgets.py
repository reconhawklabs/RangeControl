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


def test_console_renders_a_ruling(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question(RECORD)
    text = tab.dump()
    assert "opcodeoperator" in text
    assert "DENIED" in text
    assert "vendor maintenance hold" in text
    assert "breaks MSEL-INJECT-ALPHA" in text


def test_console_renders_a_deflection_without_a_verdict(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "kind": "deflection", "verdict": None,
                         "internal_reason": None, "impacted": []})
    assert "DEFLECTION" in tab.dump()


def test_console_appends_log_lines(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_log("white cell channel unreachable")
    assert "unreachable" in tab.dump()


def test_console_does_not_crash_on_a_partial_record(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({"kind": "error"})
    text = tab.dump()
    assert "None" not in text


def test_console_unknown_kind_renders_sensibly(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "kind": "unheard_of_kind", "verdict": None})
    assert "UNHEARD_OF_KIND" in tab.dump()


def test_console_renders_hitl_approved_in_green(root):
    from rangecontrol.gui.console_tab import _COLOURS, ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "kind": "hitl_approved", "verdict": None,
                         "reviewer": "blue-lead-reviewer"})
    assert "HITL_APPROVED" in tab.dump()
    assert _COLOURS["hitl_approved"] == _COLOURS["approved"]


def test_console_renders_hitl_denied_in_red(root):
    from rangecontrol.gui.console_tab import _COLOURS, ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "kind": "hitl_denied", "verdict": None,
                         "reviewer": "blue-lead-reviewer"})
    assert "HITL_DENIED" in tab.dump()
    assert _COLOURS["hitl_denied"] == _COLOURS["denied"]


def test_console_shows_the_reviewer_when_the_record_carries_one(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "kind": "hitl_denied", "verdict": None,
                         "reviewer": "blue-lead-reviewer"})
    assert "blue-lead-reviewer" in tab.dump()


def test_console_omits_the_reviewer_line_when_absent(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "kind": "ruling"})
    assert "Reviewer" not in tab.dump()


# -- I1: a held ruling must read as held, not as a delivered ruling ----------


def test_console_renders_a_held_ruling_distinctly(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question({**RECORD, "held": True, "public_response": "Approved. Go ahead."})
    text = tab.dump()
    assert "HELD" in text
    assert "not sent" in text.lower()
    # Still visible to the operator -- just labelled as awaiting release,
    # not claimed as delivered.
    assert "Approved. Go ahead." in text
    assert "Public:" not in text


def test_console_a_non_held_ruling_still_reads_public(root):
    from rangecontrol.gui.console_tab import ConsoleTab

    tab = ConsoleTab(root)
    tab.append_question(RECORD)
    assert "Public:" in tab.dump()
    assert "HELD" not in tab.dump()


def test_status_bar_shows_running_and_stats(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_bot_state(BotState(RUNNING))
    bar.set_report(REPORT)
    assert "running" in bar.dump().lower()
    assert "19" in bar.dump()
    assert "8" in bar.dump()


def test_status_bar_shows_the_failure_reason(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_bot_state(BotState(FAILED, "Discord rejected the bot token."))
    assert "rejected the bot token" in bar.dump()


def test_status_bar_shows_stopping_with_detail(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_bot_state(
        BotState(STOPPING, "the bot did not exit within the timeout")
    )
    text = bar.dump()
    assert "stopping" in text.lower() or "shutting down" in text.lower()
    assert "did not exit within the timeout" in text


def test_status_bar_unknown_state_does_not_crash(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_bot_state(BotState("mystery", "something odd"))
    text = bar.dump()
    assert "mystery" in text
    assert "something odd" in text


# -- I1: a pending count, so the operator can see rulings are waiting -------


def test_status_bar_shows_the_pending_count(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_pending(3)
    assert "3" in bar.dump()


def test_status_bar_hides_the_pending_line_at_zero(root):
    from rangecontrol.gui.statusbar import StatusBar

    bar = StatusBar(root)
    bar.set_pending(3)
    bar.set_pending(0)
    assert "pending" not in bar.dump().lower()
    assert "awaiting" not in bar.dump().lower()
