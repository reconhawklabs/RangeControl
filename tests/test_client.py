import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.models import Advice, GateVerdict
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
from rangecontrol.llm.base import LLMError
from tests.support.stub_provider import StubProvider

BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "12345",
}

PUBLIC = "No. That is waiting on the change board."


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


# The reply reviewer runs on the ruling provider, right after the ruling.
SCREEN_CLEAN = json.dumps({"discloses": False, "reason": ""})
SCREEN_DISCLOSES = json.dumps({"discloses": True, "reason": "names a host"})


def ruling_reply(public=PUBLIC):
    return json.dumps(
        {
            "verdict": "DENIED",
            "public_response": public,
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


def audit_records(bot):
    text = bot.audit.path_for_today().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def ask(bot, question="can we block 77.232.11.2"):
    return await handle_question(
        bot,
        question=question,
        user_id=1,
        user_name="blue-lead",
        channel_id=7,
        channel_name="ops",
    )


async def boom(*args, **kwargs):
    raise RuntimeError("channel not found")


def make_bot(tmp_path, errors=None, **env):
    config = load_config({**BASE_ENV, "RANGE_DIR": str(tmp_path), **env})
    on_error = errors.append if errors is not None else None
    return RangeControlBot(
        config, advisor=object(), audit=AuditLog(tmp_path), on_error=on_error
    )


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


# -- the question pipeline ----------------------------------------------------


async def test_only_allowlisted_channels_are_served(tmp_path):
    """An empty allowlist serves every channel. A slash command outside a
    non-empty one gets a private refusal: silently ignoring it makes Discord
    show 'The application did not respond', which reads as a crash."""
    for channel, allowed, expected in [(5, (), True), (5, (5, 9), True), (5, (9,), False)]:
        assert should_handle(channel, allowed) is expected, (channel, allowed)

    bot = make_bot(tmp_path, ALLOWED_CHANNEL_IDS="777")
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


async def test_a_ruling_is_answered_reported_and_audited_without_leaking(tmp_path):
    bot = build(tmp_path, [gate_reply()], [ruling_reply(), SCREEN_CLEAN])

    reply = await ask(bot)

    assert reply == PUBLIC
    assert "MSEL-01" not in reply
    assert "DC-VULCAN" not in reply

    [(advice, user_name, channel_name, _)] = bot.white_cell_posts
    assert advice.ruling.internal_reason == "breaks MSEL-01"
    assert (user_name, channel_name) == ("blue-lead", "ops")

    [record] = audit_records(bot)
    assert record["verdict"] == "DENIED"
    assert record["internal_reason"] == "breaks MSEL-01"
    assert record["latency_ms"] >= 0
    assert "held" not in record  # HITL is off by default


async def test_a_screened_draft_reaches_only_the_white_cell_and_audit(tmp_path):
    """The reply screen swaps a disclosing draft for a fallback; the blue team
    must never see the draft, while the reviewer and audit must see it and why."""
    draft = "No. That would cut DC-VULCAN off from its callback."
    bot = build(tmp_path, [gate_reply()], [ruling_reply(draft), SCREEN_DISCLOSES])

    reply = await ask(bot)

    assert "DC-VULCAN" not in reply
    assert reply != draft

    [(advice, _, _, _)] = bot.white_cell_posts
    assert advice.screen_reason
    assert advice.ruling.public_response == draft

    [record] = audit_records(bot)
    assert record["public_response"] == reply
    assert record["model_draft"] == draft
    assert "names a host" in record["screen_reason"]


async def test_every_interaction_is_audited_including_rate_limited_ones(tmp_path):
    """Denial probing is the behavior most likely to trip the limiter -- it
    must not be exactly what goes missing from the audit trail."""
    gate = StubProvider(completions=[gate_reply(GateVerdict.NOT_A_CHANGE_REQUEST)])
    advisor = Advisor(
        provider=StubProvider(), gate_provider=gate, range_md="r", corpus_text="c"
    )
    bot = FakeBot(advisor, AuditLog(tmp_path), RateLimiter(max_calls=1, per_seconds=60))

    await ask(bot, "what is behind that firewall?")
    reply = await ask(bot, "can we block 198.51.100.7")

    assert reply == RATE_LIMIT_TEXT
    assert "approv" not in reply.lower()
    assert len(gate.calls) == 1  # the limited attempt never reached a model

    deflection, limited = audit_records(bot)
    assert deflection["kind"] == "deflection"
    assert limited["kind"] == "rate_limited"
    assert limited["question"] == "can we block 198.51.100.7"
    assert limited["latency_ms"] == 0


async def test_provider_failure_yields_the_error_text(tmp_path):
    advisor = Advisor(
        provider=StubProvider(),
        gate_provider=StubProvider(error=LLMError("down")),
        range_md="r",
        corpus_text="c",
    )
    bot = FakeBot(advisor, AuditLog(tmp_path), RateLimiter(100, 60))
    assert await ask(bot) == ERROR_TEXT


async def test_advise_runs_off_the_event_loop_thread(tmp_path):
    """The provider SDKs are synchronous. Running a 30-second ruling on the
    event loop thread stalls Discord's heartbeat and every other question
    and reaction until it returns."""

    class ThreadRecordingAdvisor:
        def __init__(self):
            self.thread_ids = []

        def advise(self, question):
            self.thread_ids.append(threading.get_ident())
            return Advice(kind="deflection", public_text="nope")

    advisor = ThreadRecordingAdvisor()
    bot = FakeBot(advisor, AuditLog(tmp_path), RateLimiter(100, 60))

    assert await ask(bot) == "nope"
    assert len(advisor.thread_ids) == 1
    assert advisor.thread_ids[0] != threading.get_ident()


# -- holding for the white cell ------------------------------------------------


async def test_a_held_ruling_is_marked_held_in_the_audit(tmp_path):
    """The audit record still carries the real ruling for the after-action
    review, labelled as not yet delivered: the asker got HELD_TEXT."""
    bot = build(tmp_path, [gate_reply()], [ruling_reply(), SCREEN_CLEAN])
    bot.mode.set(True)

    async def posted(advice, **kwargs):
        return 999

    bot.post_white_cell = posted

    assert await ask(bot) == HELD_TEXT

    [record] = audit_records(bot)
    assert record["held"] is True
    assert record["public_response"] == PUBLIC


async def test_white_cell_post_failure_under_hitl_answers_directly_and_reports_it(
    tmp_path, caplog
):
    """Holding with nobody able to review would strand the asker, so the reply
    goes out unreviewed -- never marked held -- and the operator is told on the
    ERROR wire, not only in a log line that scrolls away."""
    bot = build(tmp_path, [gate_reply()], [ruling_reply(), SCREEN_CLEAN])
    bot.mode.set(True)
    bot.post_white_cell = boom

    with caplog.at_level(logging.ERROR):
        reply = await ask(bot, "can we block 77.232.11.2")

    assert reply == PUBLIC
    [record] = audit_records(bot)
    assert "held" not in record
    assert len(bot.errors) == 1
    assert "no review" in bot.errors[0].lower()
    for detail in ("blue-lead", "ops", "can we block 77.232.11.2"):
        assert detail in caplog.text


# -- startup ---------------------------------------------------------------------


async def test_ready_names_why_the_white_cell_is_unreachable(tmp_path, caplog):
    """Both the console banner and the GUI's ERROR wire must say why, so the
    operator fixes the right thing. Startup proceeds regardless."""
    errors = []
    bot = make_bot(tmp_path, errors)
    bot.get_channel = lambda channel_id: None
    bot.fetch_channel = AsyncMock(side_effect=_forbidden())

    with caplog.at_level(logging.ERROR):
        await bot.on_ready()

    assert len(errors) == 1
    assert "12345" in errors[0]
    assert "cannot see" in errors[0]
    assert "WHITE CELL" in caplog.text
    assert "cannot see" in caplog.text


async def test_ready_reports_missing_posting_permissions(tmp_path):
    """Seeing the channel is not enough: without Embed Links every ruling's
    audit embed would fail, one logger.exception at a time."""
    errors = []
    bot = make_bot(tmp_path, errors)
    bot.get_channel = lambda channel_id: _text_channel(embed_links=False)

    await bot.on_ready()

    assert errors and "Embed Links" in errors[0]


async def test_ready_warns_about_an_allowed_channel_the_bot_cannot_read(tmp_path):
    errors = []
    bot = make_bot(tmp_path, errors, ALLOWED_CHANNEL_IDS="777")
    channels = {12345: _text_channel(), 777: _text_channel("blue-1", view_channel=False)}
    bot.get_channel = lambda channel_id: channels.get(channel_id)

    await bot.on_ready()

    assert len(errors) == 1
    assert "777" in errors[0]
    assert "ignored" in errors[0]


async def test_ready_confirms_a_working_white_cell_once_across_reconnects(tmp_path, caplog):
    """Silence on success left operators unsure oversight was wired up at all;
    READY fires again on every resume and must not re-check."""
    errors = []
    bot = make_bot(tmp_path, errors)
    bot.get_channel = lambda channel_id: None
    bot.fetch_channel = AsyncMock(return_value=_text_channel())

    with caplog.at_level(logging.INFO):
        await bot.on_ready()
        await bot.on_ready()

    assert errors == []
    assert "WHITE CELL" not in caplog.text
    assert "white-cell" in caplog.text
    assert "verified" in caplog.text.lower()
    bot.fetch_channel.assert_awaited_once_with(12345)


async def test_setup_hook_does_not_block_startup_on_sync_failure(tmp_path):
    bot = make_bot(tmp_path)
    bot.tree.sync = AsyncMock(side_effect=RuntimeError("discord unreachable"))

    await bot.setup_hook()  # must not raise
