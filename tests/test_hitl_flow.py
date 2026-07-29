import json
import logging
from types import SimpleNamespace

import discord
import pytest

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
from rangecontrol.config import load_config

RULING = Ruling(
    verdict="DENIED", public_response="No, that segment is under a vendor hold.",
    internal_reason="breaks MSEL-INJECT-ALPHA", impacted=("MSEL-INJECT-ALPHA",),
    confidence="high",
)


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
        self.pending = PendingRegistry()
        self.white_cell_posts = []
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
        self.white_cell_posts.append(advice)
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


async def ask(bot, question="can we block 203.0.113.10 at the edge firewall"):
    return await handle_question(
        bot, question=question, user_id=1, user_name="blue",
        channel_id=42, channel_name="ops-blue",
    )


BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "999",
}


def real_bot(tmp_path):
    """A real RangeControlBot, never connected to a gateway.

    discord.Client.__init__ does no network I/O, so this is safe to build in
    a test -- the same pattern tests/test_client.py already uses to drive
    setup_hook. ``self.user`` is a read-only property backed by
    ConnectionState.user, which is otherwise None until login; patching that
    directly is the supported way to give the bot an identity in a test.
    """
    config = load_config({**BASE_ENV, "RANGE_DIR": str(tmp_path)})
    bot = RangeControlBot(config, advisor=object(), audit=AuditLog(tmp_path))
    bot._connection.user = SimpleNamespace(id=1, bot=True)
    bot.sent = []

    async def fake_send_to_channel(channel_id, text):
        bot.sent.append((channel_id, text))

    bot.send_to_channel = fake_send_to_channel
    return bot


def seed_pending(
    bot,
    *,
    message_id=555,
    channel_id=42,
    user_id=1,
    user_name="blue",
    question="can we block 203.0.113.10 at the edge firewall",
    public_text=RULING.public_response,
):
    bot.pending.add(
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


class FakeMember:
    def __init__(self, display_name, *, bot=False):
        self.display_name = display_name
        self.bot = bot


def raw_reaction(*, message_id, channel_id, user_id, emoji, member=None):
    """Build a discord.RawReactionActionEvent with no gateway involved.

    RawReactionActionEvent.__init__ only touches a plain dict, a PartialEmoji,
    and a string -- no connection required.
    """
    payload = discord.RawReactionActionEvent(
        {"message_id": message_id, "channel_id": channel_id, "user_id": user_id, "type": 0},
        discord.PartialEmoji(name=emoji),
        "REACTION_ADD",
    )
    payload.member = member
    return payload


@pytest.mark.asyncio
async def test_mode_off_answers_immediately(tmp_path):
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=False, tmp_path=tmp_path)
    assert await ask(bot) == RULING.public_response
    assert len(bot.pending) == 0


@pytest.mark.asyncio
async def test_mode_on_holds_the_ruling(tmp_path):
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    reply = await ask(bot)
    assert reply == HELD_TEXT
    assert RULING.public_response not in reply
    assert len(bot.pending) == 1


@pytest.mark.asyncio
async def test_a_deflection_is_never_held(tmp_path):
    """Recon questions carry no adjudication to review."""
    bot = FakeBot(Advice(kind="deflection", public_text="I only handle changes."),
                  hitl=True, tmp_path=tmp_path)
    assert await ask(bot) == "I only handle changes."
    assert len(bot.pending) == 0


@pytest.mark.asyncio
async def test_an_error_is_never_held(tmp_path):
    bot = FakeBot(Advice(kind="error", public_text="I can't process that."),
                  hitl=True, tmp_path=tmp_path)
    assert await ask(bot) == "I can't process that."
    assert len(bot.pending) == 0


@pytest.mark.asyncio
async def test_approving_releases_the_original_reply(tmp_path):
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)
    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)
    # Prefixed with a mention (see test_release_mentions_the_asker below) --
    # asserted here as "the original reply, addressed to the asker" rather
    # than a bare string, so the exact tag format lives in one place.
    assert bot.sent == [(42, f"<@1> {RULING.public_response}")]


@pytest.mark.asyncio
async def test_denying_sends_the_fixed_refusal_only(tmp_path):
    """The refusal must never carry any part of the real ruling."""
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)
    await release_pending(bot, approval_message_id=555, emoji=DENY_EMOJI)
    assert bot.sent == [(42, f"<@1> {HITL_DENIED_TEXT}")]
    assert RULING.public_response not in HITL_DENIED_TEXT
    assert RULING.internal_reason not in HITL_DENIED_TEXT


@pytest.mark.asyncio
async def test_release_truncates_after_adding_the_mention(tmp_path):
    """format_public already truncates the ruling text to exactly
    DISCORD_LIMIT; release_pending then prepends a mention on top of that,
    which -- unless re-truncated -- pushes the composed string past
    Discord's own ceiling, so channel.send raises and the request (already
    taken out of pending) is answered to nobody."""
    from rangecontrol.bot.responder import DISCORD_LIMIT

    at_the_limit = Ruling(
        verdict="APPROVED", public_response="x" * (DISCORD_LIMIT + 500),
        internal_reason="fine", impacted=(), confidence="high",
    )
    bot = FakeBot(
        Advice(kind="ruling", public_text=at_the_limit.public_response,
               ruling=at_the_limit),
        hitl=True, tmp_path=tmp_path,
    )
    await ask(bot)  # public_text stored in pending is already format_public()'d
    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)

    sent_channel, sent_text = bot.sent[0]
    assert len(sent_text) <= DISCORD_LIMIT
    assert sent_text.startswith("<@1> ")


@pytest.mark.asyncio
async def test_release_mentions_the_asker(tmp_path):
    """With several blue teamers in one channel, an unaddressed release is
    ambiguous -- prefix every release with the original asker's mention. A
    mention carries no range knowledge, so this is free to add unconditionally
    to both an approval and a denial."""
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)  # user_id=1, per the ask() helper above
    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)
    assert bot.sent[0][1].startswith("<@1> ")


@pytest.mark.asyncio
async def test_a_second_reaction_releases_nothing(tmp_path):
    """Two reviewers must not produce two replies."""
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)
    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)
    await release_pending(bot, approval_message_id=555, emoji=APPROVE_EMOJI)
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_an_unrelated_emoji_does_nothing(tmp_path):
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)
    await release_pending(bot, approval_message_id=555, emoji="🎉")
    assert bot.sent == []
    assert len(bot.pending) == 1


@pytest.mark.asyncio
async def test_the_human_decision_is_audited(tmp_path):
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)
    await release_pending(bot, approval_message_id=555, emoji=DENY_EMOJI)
    lines = (list(tmp_path.glob("audit-*.jsonl"))[0]).read_text(encoding="utf-8")
    assert "hitl_denied" in lines


@pytest.mark.asyncio
async def test_a_failed_release_still_writes_the_audit_record(tmp_path):
    """A contested denial that never reached the asker is exactly the case an
    after-action review needs the trail for most -- losing it here would
    lose the only remaining record the decision was ever made."""
    bot = FakeBot(Advice(kind="ruling", public_text=RULING.public_response,
                         ruling=RULING), hitl=True, tmp_path=tmp_path)
    await ask(bot)

    async def boom(channel_id, text):
        raise RuntimeError("channel not found")

    bot.send_to_channel = boom

    await release_pending(bot, approval_message_id=555, emoji=DENY_EMOJI)  # must not raise

    assert len(bot.pending) == 0  # the request was still taken, not left double-held
    lines = (list(tmp_path.glob("audit-*.jsonl"))[0]).read_text(encoding="utf-8")
    assert "hitl_denied" in lines


@pytest.mark.asyncio
async def test_a_reaction_outside_the_white_cell_does_not_release(tmp_path):
    bot = real_bot(tmp_path)
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=1,  # a blue-team channel, not the white cell (999)
        user_id=2,
        emoji=APPROVE_EMOJI,
        member=FakeMember("blue-team-member"),
    ))

    assert bot.sent == []
    assert len(bot.pending) == 1


@pytest.mark.asyncio
async def test_the_bots_own_reaction_does_not_release(tmp_path):
    bot = real_bot(tmp_path)
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=bot.config.white_cell_channel_id,
        user_id=bot.user.id,
        emoji=APPROVE_EMOJI,
        member=FakeMember("RangeControl", bot=True),
    ))

    assert bot.sent == []
    assert len(bot.pending) == 1


@pytest.mark.asyncio
async def test_a_different_bots_reaction_does_not_release(tmp_path):
    """Only self-reactions were guarded before; any bot in the channel
    (a logging bot, a moderation bot) must be ignored the same way."""
    bot = real_bot(tmp_path)
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=bot.config.white_cell_channel_id,
        user_id=777,
        emoji=APPROVE_EMOJI,
        member=FakeMember("SomeOtherBot", bot=True),
    ))

    assert bot.sent == []
    assert len(bot.pending) == 1


@pytest.mark.asyncio
async def test_an_unknown_bot_identity_does_not_release(tmp_path):
    """Regression guard for on_raw_reaction_add's first condition:

        if self.user is None or payload.user_id == self.user.id: return

    This is fail-closed on purpose -- discord.py's ``self.user`` is None
    until login completes, and treating "we don't know who we are yet" as
    "definitely not us" would be a security boundary quietly failing open.
    Every other test in this module leaves ``self.user`` set; this is the
    only one covering the ``is None`` half, so a regression that flips the
    polarity back to fail-open (e.g. ``self.user is not None and ...``)
    would otherwise pass every other test here and go live undetected.
    Uses the same channel, user, and emoji as the positive control below --
    the only difference is the unset identity -- so a fail-open regression
    is the only way this could release the request.
    """
    bot = real_bot(tmp_path)
    bot._connection.user = None
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=bot.config.white_cell_channel_id,
        user_id=2,
        emoji=APPROVE_EMOJI,
        member=FakeMember("blue-lead-reviewer"),
    ))

    assert bot.sent == []
    assert len(bot.pending) == 1


@pytest.mark.asyncio
async def test_a_white_cell_reaction_from_a_reviewer_releases_it(tmp_path):
    """Positive control: proves the three guard tests above are not passing
    for the wrong reason (e.g. a bug that blocks every release)."""
    bot = real_bot(tmp_path)
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=bot.config.white_cell_channel_id,
        user_id=2,
        emoji=APPROVE_EMOJI,
        member=FakeMember("blue-lead-reviewer"),
    ))

    assert bot.sent == [(42, f"<@1> {RULING.public_response}")]  # seed_pending's user_id=1
    assert len(bot.pending) == 0


@pytest.mark.asyncio
async def test_the_reviewer_name_reaches_the_audit_record(tmp_path):
    bot = real_bot(tmp_path)
    seed_pending(bot)

    await bot.on_raw_reaction_add(raw_reaction(
        message_id=555,
        channel_id=bot.config.white_cell_channel_id,
        user_id=2,
        emoji=DENY_EMOJI,
        member=FakeMember("blue-lead-reviewer"),
    ))

    line = bot.audit.path_for_today().read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["reviewer"] == "blue-lead-reviewer"


@pytest.mark.asyncio
async def test_the_hold_is_registered_before_the_reactions_go_up(tmp_path):
    """A reviewer reacting in the gap between the message appearing and the
    reactions appearing on it must still find something to release."""
    bot = real_bot(tmp_path)
    seen_pending_length_during_add_reaction = []

    class FakeMessage:
        id = 777

        async def add_reaction(self, emoji):
            seen_pending_length_during_add_reaction.append(len(bot.pending))

    class FakeChannel:
        async def send(self, embed):
            return FakeMessage()

    bot.get_channel = lambda channel_id: FakeChannel()

    await bot.post_white_cell(
        Advice(kind="ruling", public_text=RULING.public_response, ruling=RULING),
        user_id=1,
        user_name="blue",
        channel_id=42,
        channel_name="ops-blue",
        question="can we block 203.0.113.10 at the edge firewall",
        public_text=RULING.public_response,
        hitl_enabled=True,
    )

    # Both reactions saw the hold already registered -- never zero.
    assert seen_pending_length_during_add_reaction == [1, 1]


@pytest.mark.asyncio
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
        result = await bot.post_white_cell(
            Advice(kind="ruling", public_text=RULING.public_response, ruling=RULING),
            user_id=1,
            user_name="blue",
            channel_id=42,
            channel_name="ops-blue",
            question="can we block 203.0.113.10 at the edge firewall",
            public_text=RULING.public_response,
            hitl_enabled=True,
        )

    assert result == 888
    assert len(bot.pending) == 1
    assert "reaction" in caplog.text.lower()
