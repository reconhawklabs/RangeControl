import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import pytest

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.models import GateVerdict
from rangecontrol.advisor.prompts import ERROR_TEXT
from rangecontrol.audit.log import AuditLog
from rangecontrol.bot.diagnostics import WHITE_CELL_PERMISSIONS
from rangecontrol.bot.client import (
    HELD_TEXT,
    RATE_LIMIT_TEXT,
    RangeControlBot,
    handle_question,
    should_handle,
)
from rangecontrol.bot.mode import ModeSwitch
from rangecontrol.bot.pending import PendingRegistry
from rangecontrol.bot.ratelimit import RateLimiter
from rangecontrol.config import DEFAULT_HITL_DENIED_TEXT, load_config
from tests.support.stub_provider import StubProvider

BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "12345",
}


def config_for(tmp_path):
    return load_config({**BASE_ENV, "RANGE_DIR": str(tmp_path)})


class FakeBot:
    """Stands in for RangeControlBot without a gateway connection."""

    def __init__(self, advisor, audit, limiter):
        self.advisor = advisor
        self.audit = audit
        self.limiter = limiter
        self.mode = ModeSwitch(False)
        self.denied_text = DEFAULT_HITL_DENIED_TEXT
        self.pending = PendingRegistry()
        self.white_cell_posts = []
        self.sent = []
        self.errors = []

    def report_error(self, text):
        self.errors.append(text)

    async def post_white_cell(
        self,
        advice,
        *,
        user_id,
        user_name,
        channel_id,
        channel_name,
        question,
        public_text,
        hitl_enabled,
    ):
        self.white_cell_posts.append((advice, user_name, channel_name, question))
        return None

    async def send_to_channel(self, channel_id, text):
        self.sent.append((channel_id, text))


def gate_reply(verdict=GateVerdict.CHANGE_REQUEST):
    return json.dumps({"verdict": verdict, "reason": "r"})


def ruling_reply():
    return json.dumps(
        {
            "verdict": "DENIED",
            "public_response": "No. A partner service depends on it.",
            "internal_reason": "breaks MSEL-01",
            "impacted": ["MSEL-01"],
            "confidence": "high",
        }
    )


def build(tmp_path, gate_completions, ruling_completions=None, limiter=None):
    advisor = Advisor(
        provider=StubProvider(completions=ruling_completions or []),
        gate_provider=StubProvider(completions=gate_completions),
        range_md="## Protected Dependency Index\n| DC-VULCAN |",
        corpus_text="10.77.0.0/16",
    )
    return FakeBot(advisor, AuditLog(tmp_path), limiter or RateLimiter(100, 60))


@pytest.mark.parametrize(
    "channel,allowed,expected",
    [(5, (), True), (5, (5, 9), True), (5, (9,), False)],
)
def test_channel_allowlist(channel, allowed, expected):
    assert should_handle(channel, allowed) is expected


async def ask(bot, question="can we block 77.232.11.2"):
    return await handle_question(
        bot,
        question=question,
        user_id=1,
        user_name="blue-lead",
        channel_id=7,
        channel_name="ops",
    )


async def test_returns_the_public_reply(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])
    assert await ask(bot) == "No. A partner service depends on it."


async def test_posts_to_the_white_cell(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])
    await ask(bot)
    assert len(bot.white_cell_posts) == 1
    advice, user_name, channel_name, question = bot.white_cell_posts[0]
    assert advice.ruling.internal_reason == "breaks MSEL-01"
    assert user_name == "blue-lead"
    assert channel_name == "ops"


async def test_public_reply_never_carries_internal_detail(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])
    reply = await ask(bot)
    assert "MSEL-01" not in reply
    assert "DC-VULCAN" not in reply


async def test_writes_an_audit_record(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])
    await ask(bot)
    line = bot.audit.path_for_today().read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["verdict"] == "DENIED"
    assert record["latency_ms"] >= 0


async def test_deflections_are_audited_too(tmp_path):
    bot = build(tmp_path, [gate_reply(GateVerdict.NOT_A_CHANGE_REQUEST)])
    await ask(bot, "what is behind that firewall?")
    record = json.loads(bot.audit.path_for_today().read_text(encoding="utf-8").strip())
    assert record["kind"] == "deflection"


async def test_rate_limited_user_gets_a_notice_and_no_model_call(tmp_path):
    limiter = RateLimiter(max_calls=1, per_seconds=60)
    bot = build(tmp_path, [gate_reply(), gate_reply()], [ruling_reply()], limiter=limiter)
    await ask(bot)
    assert await ask(bot) == RATE_LIMIT_TEXT


async def test_rate_limit_notice_is_not_an_approval(tmp_path):
    limiter = RateLimiter(max_calls=0, per_seconds=60)
    bot = build(tmp_path, [], limiter=limiter)
    reply = await ask(bot)
    assert "approv" not in reply.lower()


async def test_rate_limited_request_is_still_audited(tmp_path):
    """Denial probing is the behavior most likely to trip the limiter — it
    must not be exactly what goes missing from the audit trail."""
    limiter = RateLimiter(max_calls=0, per_seconds=60)
    bot = build(tmp_path, [], limiter=limiter)
    await ask(bot, "can we block 198.51.100.7")
    line = bot.audit.path_for_today().read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["kind"] == "rate_limited"
    assert record["latency_ms"] == 0
    assert record["question"] == "can we block 198.51.100.7"


async def test_provider_failure_yields_the_error_text(tmp_path):
    from rangecontrol.llm.base import LLMError

    advisor = Advisor(
        provider=StubProvider(),
        gate_provider=StubProvider(error=LLMError("down")),
        range_md="r",
        corpus_text="c",
    )
    bot = FakeBot(advisor, AuditLog(tmp_path), RateLimiter(100, 60))
    assert await ask(bot) == ERROR_TEXT


async def test_white_cell_post_failure_does_not_break_the_reply(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])

    async def boom(*args, **kwargs):
        raise RuntimeError("channel not found")

    bot.post_white_cell = boom
    assert await ask(bot) == "No. A partner service depends on it."


async def test_white_cell_post_failure_logs_who_and_what(tmp_path, caplog):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])

    async def boom(*args, **kwargs):
        raise RuntimeError("channel not found")

    bot.post_white_cell = boom
    with caplog.at_level(logging.ERROR):
        await ask(bot, "can we block 77.232.11.2")

    assert "blue-lead" in caplog.text
    assert "ops" in caplog.text
    assert "can we block 77.232.11.2" in caplog.text


# -- I1: a held ruling must be marked held in the audit trail ----------------


async def test_a_held_ruling_is_marked_held_in_the_audit(tmp_path):
    """Before this fix, the audit record was written before the hold
    decision existed, so it always looked like the ruling text went out --
    even though the blue team actually got HELD_TEXT."""
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])
    bot.mode.set(True)

    async def fake_post_white_cell(advice, **kwargs):
        return 999  # pretend the white cell post succeeded

    bot.post_white_cell = fake_post_white_cell

    reply = await ask(bot)
    assert reply == HELD_TEXT

    record = json.loads(bot.audit.path_for_today().read_text(encoding="utf-8").strip())
    assert record["held"] is True
    # The audit record still carries the real ruling (for the eventual
    # after-action review), it is just now labelled as not yet delivered.
    assert record["public_response"] == "No. A partner service depends on it."
    assert record["public_response"] != reply


async def test_a_ruling_answered_immediately_is_not_marked_held(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])  # HITL off by default
    await ask(bot)
    record = json.loads(bot.audit.path_for_today().read_text(encoding="utf-8").strip())
    assert "held" not in record


# -- I2: a failed white cell post under HITL must raise an ERROR, not just --
# -- a log line that scrolls away --------------------------------------------


async def test_hitl_fail_open_is_not_marked_held_and_reports_an_error(tmp_path):
    """FakeBot.post_white_cell returns None unconditionally, so enabling HITL
    here reproduces "the post failed" without needing to fake an exception:
    the reply must go out unreviewed (fail-open, matching HITL off), never
    marked held, and the operator must be told via report_error -- the wire
    this test proves is actually connected."""
    bot = build(tmp_path, [gate_reply()], [ruling_reply()])
    bot.mode.set(True)

    reply = await ask(bot)
    assert reply == "No. A partner service depends on it."  # answered, unreviewed

    record = json.loads(bot.audit.path_for_today().read_text(encoding="utf-8").strip())
    assert "held" not in record

    assert bot.errors
    assert "no review" in bot.errors[0].lower()


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "x")


def _text_channel(name="white-cell", **denied):
    granted = {flag: True for flag in WHITE_CELL_PERMISSIONS}
    granted.update(denied)
    perms = SimpleNamespace(**granted)
    return SimpleNamespace(
        id=12345, name=name, guild=SimpleNamespace(name="Range", me=object()),
        permissions_for=lambda member: perms,
    )


async def test_ready_reports_an_error_when_the_white_cell_is_unreachable(tmp_path):
    """The white-cell-unreachable condition must reach the ERROR wire, not
    only the logger.error banner that scrolls off the console feed -- and it
    must say why, so the operator can fix the right thing."""
    errors = []
    bot = RangeControlBot(
        config_for(tmp_path), advisor=object(), audit=AuditLog(tmp_path),
        on_error=errors.append,
    )
    bot.get_channel = lambda channel_id: None
    bot.fetch_channel = AsyncMock(side_effect=_forbidden())

    await bot.on_ready()  # must not raise -- startup proceeds regardless

    assert errors
    assert "12345" in errors[0]
    assert "cannot see" in errors[0]


async def test_ready_reports_nothing_when_the_white_cell_resolves(tmp_path):
    errors = []
    bot = RangeControlBot(
        config_for(tmp_path), advisor=object(), audit=AuditLog(tmp_path),
        on_error=errors.append,
    )
    bot.get_channel = lambda channel_id: _text_channel()
    bot.fetch_channel = AsyncMock()

    await bot.on_ready()

    assert errors == []


async def test_ready_reports_missing_posting_permissions(tmp_path):
    """Seeing the channel is not enough: without Embed Links every ruling's
    audit embed would fail, one logger.exception at a time."""
    errors = []
    bot = RangeControlBot(
        config_for(tmp_path), advisor=object(), audit=AuditLog(tmp_path),
        on_error=errors.append,
    )
    bot.get_channel = lambda channel_id: _text_channel(embed_links=False)

    await bot.on_ready()

    assert errors and "Embed Links" in errors[0]


async def test_ready_warns_about_an_allowed_channel_the_bot_cannot_read(tmp_path):
    errors = []
    config = load_config(
        {**BASE_ENV, "RANGE_DIR": str(tmp_path), "ALLOWED_CHANNEL_IDS": "777"}
    )
    bot = RangeControlBot(
        config, advisor=object(), audit=AuditLog(tmp_path), on_error=errors.append,
    )
    channels = {12345: _text_channel(), 777: _text_channel("blue-1", view_channel=False)}
    bot.get_channel = lambda channel_id: channels.get(channel_id)

    await bot.on_ready()

    assert errors
    assert "777" in errors[0]
    assert "ignored" in errors[0]


async def test_ready_checks_channels_only_once_across_reconnects(tmp_path):
    bot = RangeControlBot(config_for(tmp_path), advisor=object(), audit=AuditLog(tmp_path))
    bot.get_channel = lambda channel_id: None
    bot.fetch_channel = AsyncMock(return_value=_text_channel())

    await bot.on_ready()
    await bot.on_ready()

    bot.fetch_channel.assert_awaited_once_with(12345)


def real_bot(tmp_path):
    advisor = Advisor(
        provider=StubProvider(),
        gate_provider=StubProvider(),
        range_md="## Protected Dependency Index\n| DC-VULCAN |",
        corpus_text="10.77.0.0/16",
    )
    return RangeControlBot(config_for(tmp_path), advisor, AuditLog(tmp_path))


async def test_ready_logs_a_banner_with_the_reason_when_unreachable(tmp_path, caplog):
    bot = real_bot(tmp_path)
    bot.get_channel = lambda channel_id: None
    bot.fetch_channel = AsyncMock(side_effect=_forbidden())

    with caplog.at_level(logging.ERROR):
        await bot.on_ready()  # must not raise -- startup proceeds regardless

    bot.fetch_channel.assert_awaited_once_with(12345)
    assert "WHITE CELL" in caplog.text
    assert "12345" in caplog.text
    assert "cannot see" in caplog.text


async def test_ready_confirms_the_white_cell_channel_when_it_works(tmp_path, caplog):
    """Silence on success left operators unsure whether oversight was wired
    up at all; a positive line names the channel that was verified."""
    bot = real_bot(tmp_path)
    bot.get_channel = lambda channel_id: _text_channel()

    with caplog.at_level(logging.INFO):
        await bot.on_ready()

    assert "WHITE CELL" not in caplog.text
    assert "white-cell" in caplog.text
    assert "verified" in caplog.text.lower()


async def test_setup_hook_does_not_block_startup_on_sync_failure(tmp_path, caplog):
    bot = real_bot(tmp_path)
    bot.tree.sync = AsyncMock(side_effect=RuntimeError("discord unreachable"))

    await bot.setup_hook()  # must not raise


class ThreadRecordingAdvisor:
    """Records which thread advise() ran on."""

    def __init__(self):
        self.thread_ids = []

    def advise(self, question):
        from rangecontrol.advisor.models import Advice

        self.thread_ids.append(threading.get_ident())
        return Advice(kind="deflection", public_text="nope")


async def test_advise_runs_off_the_event_loop_thread(tmp_path):
    """The provider SDKs are synchronous. Running a 30-second ruling on the
    event loop thread stalls Discord's heartbeat and every other question
    and reaction until it returns."""
    advisor = ThreadRecordingAdvisor()
    bot = FakeBot(advisor, AuditLog(tmp_path), RateLimiter(100, 60))

    assert await ask(bot) == "nope"

    assert advisor.thread_ids == [advisor.thread_ids[0]]
    assert advisor.thread_ids[0] != threading.get_ident()


async def test_slash_command_in_a_disallowed_channel_gets_an_ephemeral_refusal(tmp_path):
    """Silently ignoring the interaction makes Discord show 'The application
    did not respond', which reads as a crash rather than a scope decision."""
    config = load_config(
        {**BASE_ENV, "RANGE_DIR": str(tmp_path), "ALLOWED_CHANNEL_IDS": "777"}
    )
    bot = RangeControlBot(config, advisor=object(), audit=AuditLog(tmp_path))
    interaction = SimpleNamespace(
        channel_id=999,
        channel=SimpleNamespace(name="elsewhere"),
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await bot.handle_slash(interaction, "can we block 203.0.113.10")

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_awaited()
