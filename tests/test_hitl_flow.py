import json
import logging
from types import SimpleNamespace

import discord

from rangecontrol.advisor.models import Advice, Ruling
from rangecontrol.audit.log import AuditLog
from rangecontrol.bot.client import (
    APPROVE_EMOJI,
    DENY_EMOJI,
    HELD_TEXT,
    HITL_DENIED_TEXT,
    RangeControlBot,
    handle_question,
    release_pending,
)
from rangecontrol.bot.mode import ModeSwitch
from rangecontrol.bot.pending import PendingRegistry, PendingRequest
from rangecontrol.bot.ratelimit import RateLimiter
from rangecontrol.bot.responder import DISCORD_LIMIT
from rangecontrol.config import load_config

RULING = Ruling(
    verdict="DENIED", public_response="No, that segment is under a vendor hold.",
    internal_reason="breaks MSEL-INJECT-ALPHA", impacted=("MSEL-INJECT-ALPHA",),
    confidence="high",
)

QUESTION = "can we block 203.0.113.10 at the edge firewall"


def ruling_advice(ruling=RULING):
    return Advice(kind="ruling", public_text=ruling.public_response, ruling=ruling)


class FakeAdvisor:
    def __init__(self, advice):
        self._advice = advice

    def advise(self, question):
        return self._advice


class FakeBot:
    def __init__(self, advice, *, hitl: bool, tmp_path):
        self.advisor = FakeAdvisor(advice)
        self.audit = AuditLog(tmp_path)
        self.limiter = RateLimiter()
        self.mode = ModeSwitch(hitl)
        self.denied_text = HITL_DENIED_TEXT
        self.pending = PendingRegistry()
        self.sent = []

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
        message_id = 555  # the approval message id
        # Mirrors RangeControlBot.post_white_cell: the hold is registered by
        # whoever posts the message, using the same hitl_enabled value the
        # caller already decided with -- not a fresh read of bot.mode.
        if hitl_enabled and advice.kind == "ruling":
            self.pending.add(
                message_id,
                PendingRequest(
                    channel_id=channel_id,
                    message_id=message_id,
                    user_id=user_id,
                    user_name=user_name,
                    question=question,
                    public_text=public_text,
                ),
            )
        return message_id

    async def send_to_channel(self, channel_id, text):
        self.sent.append((channel_id, text))

    def report_error(self, text):
        pass


async def ask(bot, question=QUESTION):
    return await handle_question(
        bot, question=question, user_id=1, user_name="blue",
        channel_id=42, channel_name="ops-blue",
    )


def audit_text(tmp_path):
    return "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("audit-*.jsonl"))


BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "999",
}


def real_bot(tmp_path):
    """A real RangeControlBot, never connected to a gateway.

    discord.Client.__init__ does no network I/O. ``self.user`` is a read-only
    property backed by ConnectionState.user, which is otherwise None until
    login; patching that is the supported way to give the bot an identity.
    """
    config = load_config({**BASE_ENV, "RANGE_DIR": str(tmp_path)})
    bot = RangeControlBot(config, advisor=object(), audit=AuditLog(tmp_path))
    bot._connection.user = SimpleNamespace(id=1, bot=True)
    bot.sent = []

    async def fake_send_to_channel(channel_id, text):
        bot.sent.append((channel_id, text))

    bot.send_to_channel = fake_send_to_channel
    return bot


def seed_pending(bot):
    bot.pending.add(
        555,
        PendingRequest(
            channel_id=42, message_id=555, user_id=1, user_name="blue",
            question=QUESTION, public_text=RULING.public_response,
        ),
    )


class FakeMember:
    def __init__(self, display_name, *, bot=False):
        self.display_name = display_name
        self.bot = bot


def raw_reaction(*, message_id, channel_id, user_id, emoji, member=None):
    """Build a discord.RawReactionActionEvent with no gateway involved."""
    payload = discord.RawReactionActionEvent(
        {"message_id": message_id, "channel_id": channel_id, "user_id": user_id, "type": 0},
        discord.PartialEmoji(name=emoji),
        "REACTION_ADD",
    )
    payload.member = member
    return payload


async def post_held_ruling(bot):
    return await bot.post_white_cell(
        ruling_advice(),
        user_id=1,
        user_name="blue",
        channel_id=42,
        channel_name="ops-blue",
        question=QUESTION,
        public_text=RULING.public_response,
        hitl_enabled=True,
    )


# -- holding and releasing -----------------------------------------------------


async def test_a_held_ruling_is_released_exactly_once_on_approval(tmp_path):
    """The asker gets HELD_TEXT; an unrelated emoji does nothing; ✅ releases
    the stored reply addressed to the asker; a second reaction from another
    reviewer, even the opposite one, produces no second reply."""
    bot = FakeBot(ruling_advice(), hitl=True, tmp_path=tmp_path)

    reply = await ask(bot)
    assert reply == HELD_TEXT
    assert RULING.public_response not in reply
    assert len(bot.pending) == 1

    await release_pending(bot, approval_message_id=555, emoji="🎉")
    assert bot.sent == []
    assert len(bot.pending) == 1

    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)
    await release_pending(bot, approval_message_id=555, emoji=DENY_EMOJI)

    assert bot.sent == [(42, f"<@1> {RULING.public_response}")]
    assert "hitl_approved" in audit_text(tmp_path)
    assert "hitl_denied" not in audit_text(tmp_path)


async def test_only_rulings_are_held(tmp_path):
    """Deflections, clarifications and errors carry no adjudication to review."""
    for kind, text in [
        ("deflection", "I only handle changes."),
        ("clarify", "Which address, and where?"),
        ("error", "I can't process that."),
    ]:
        bot = FakeBot(Advice(kind=kind, public_text=text), hitl=True, tmp_path=tmp_path)
        assert await ask(bot) == text, kind
        assert len(bot.pending) == 0, kind


async def test_the_mode_is_read_once_per_question(tmp_path):
    """The GUI can flip the mode mid-question. A second read could produce a
    held request with HELD_TEXT never sent, or HELD_TEXT with nothing held."""

    class FlippingMode:
        def __init__(self, first):
            self.first = first
            self.reads = 0

        @property
        def enabled(self):
            self.reads += 1
            return self.first if self.reads == 1 else not self.first

    for first in (True, False):
        bot = FakeBot(ruling_advice(), hitl=first, tmp_path=tmp_path)
        bot.mode = FlippingMode(first)

        reply = await ask(bot)

        assert bot.mode.reads == 1, first
        assert (reply == HELD_TEXT) is first
        assert len(bot.pending) == (1 if first else 0)


async def test_denial_sends_only_the_fixed_text(tmp_path):
    """The white cell can reword the denial, but it is fixed per bot and never
    derived from the ruling, so it carries nothing the ruling knew."""
    for denied_text in (HITL_DENIED_TEXT, "Change board says no for now."):
        bot = FakeBot(ruling_advice(), hitl=True, tmp_path=tmp_path)
        bot.denied_text = denied_text
        await ask(bot)

        await release_pending(bot, approval_message_id=555, emoji=DENY_EMOJI)

        assert bot.sent == [(42, f"<@1> {denied_text}")]
        assert RULING.public_response not in bot.sent[0][1]
        assert "MSEL-INJECT-ALPHA" not in bot.sent[0][1]


async def test_a_failed_release_still_writes_the_audit_record(tmp_path):
    """A contested denial that never reached the asker is exactly the case an
    after-action review needs the trail for most."""
    bot = FakeBot(ruling_advice(), hitl=True, tmp_path=tmp_path)
    await ask(bot)

    async def boom(channel_id, text):
        raise RuntimeError("channel not found")

    bot.send_to_channel = boom

    await release_pending(bot, approval_message_id=555, emoji=DENY_EMOJI)  # must not raise

    assert len(bot.pending) == 0  # taken, not left double-held
    assert "hitl_denied" in audit_text(tmp_path)


async def test_release_truncates_after_adding_the_mention(tmp_path):
    """The stored reply can already sit at exactly DISCORD_LIMIT; prepending
    the mention without re-truncating makes channel.send raise, and the
    request, already out of pending, is answered to nobody."""
    at_the_limit = Ruling(
        verdict="APPROVED", public_response="x" * (DISCORD_LIMIT + 500),
        internal_reason="fine", impacted=(), confidence="high",
    )
    bot = FakeBot(ruling_advice(at_the_limit), hitl=True, tmp_path=tmp_path)
    await ask(bot)

    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)

    [(_, sent_text)] = bot.sent
    assert len(sent_text) <= DISCORD_LIMIT
    assert sent_text.startswith("<@1> ")


# -- who may release -----------------------------------------------------------


async def test_reactions_that_must_not_release_a_hold(tmp_path):
    """Only a human in the white cell can release. Unknown own identity
    (self.user is None before login) fails closed rather than being read as
    "definitely not me"."""
    white_cell = 999
    cases = [
        ("outside the white cell", True,
         dict(channel_id=1, user_id=2, member=FakeMember("blue-team-member"))),
        ("the bot's own reaction", True,
         dict(channel_id=white_cell, user_id=1, member=FakeMember("RangeControl", bot=True))),
        ("another bot's reaction", True,
         dict(channel_id=white_cell, user_id=777, member=FakeMember("LogBot", bot=True))),
        ("own identity not yet known", False,
         dict(channel_id=white_cell, user_id=2, member=FakeMember("reviewer"))),
    ]
    for label, identity_known, event in cases:
        bot = real_bot(tmp_path)
        if not identity_known:
            bot._connection.user = None
        seed_pending(bot)

        await bot.on_raw_reaction_add(
            raw_reaction(message_id=555, emoji=APPROVE_EMOJI, **event)
        )

        assert bot.sent == [], label
        assert len(bot.pending) == 1, label


async def test_a_white_cell_reviewer_releases_it_and_is_audited(tmp_path):
    """Positive control for the guards above, through the real handler."""
    bot = real_bot(tmp_path)
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=bot.config.white_cell_channel_id,
        user_id=2,
        emoji=APPROVE_EMOJI,
        member=FakeMember("blue-lead-reviewer"),
    ))

    assert bot.sent == [(42, f"<@1> {RULING.public_response}")]
    assert len(bot.pending) == 0
    record = json.loads(bot.audit.path_for_today().read_text(encoding="utf-8").strip())
    assert record["kind"] == "hitl_approved"
    assert record["reviewer"] == "blue-lead-reviewer"


# -- posting the hold -------------------------------------------------------------


async def test_the_hold_is_registered_before_the_reactions_go_up(tmp_path):
    """A reviewer reacting in the gap between the message appearing and the
    reactions appearing on it must still find something to release."""
    bot = real_bot(tmp_path)
    pending_seen_by_add_reaction = []

    class FakeMessage:
        id = 777

        async def add_reaction(self, emoji):
            pending_seen_by_add_reaction.append(len(bot.pending))

    class FakeChannel:
        async def send(self, embed):
            return FakeMessage()

    bot.get_channel = lambda channel_id: FakeChannel()

    await post_held_ruling(bot)

    assert pending_seen_by_add_reaction == [1, 1]


async def test_a_failed_add_reaction_still_holds_and_logs(tmp_path, caplog):
    """Missing the Add Reactions permission must not undo a hold that
    already exists, and must not pass silently."""
    bot = real_bot(tmp_path)

    class FakeMessage:
        id = 888

        async def add_reaction(self, emoji):
            raise RuntimeError("missing Add Reactions permission")

    class FakeChannel:
        async def send(self, embed):
            return FakeMessage()

    bot.get_channel = lambda channel_id: FakeChannel()

    with caplog.at_level(logging.ERROR):
        result = await post_held_ruling(bot)

    assert result == 888
    assert len(bot.pending) == 1
    assert "reaction" in caplog.text.lower()
