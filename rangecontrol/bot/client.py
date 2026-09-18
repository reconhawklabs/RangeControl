"""Discord gateway, slash command, and mention handling.

The adjudication pipeline lives in the free function ``handle_question`` so it
can be exercised without a gateway connection.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Protocol

import discord
from discord import app_commands

from rangecontrol.advisor.engine import Advisor
from rangecontrol.advisor.models import Advice
from rangecontrol.audit.log import AuditLog
from rangecontrol.bot.diagnostics import (
    ALLOWED_CHANNEL_PERMISSIONS,
    WHITE_CELL_PERMISSIONS,
    ChannelCheck,
    check_channel,
)
from rangecontrol.bot.mode import ModeSwitch
from rangecontrol.bot.pending import PendingRegistry, PendingRequest
from rangecontrol.bot.ratelimit import RateLimiter
from rangecontrol.bot.responder import DISCORD_LIMIT, _truncate, format_public
from rangecontrol.bot.whitecell import build_embed
from rangecontrol.config import DEFAULT_HITL_DENIED_TEXT, Config

logger = logging.getLogger(__name__)

RATE_LIMIT_TEXT = (
    "You're sending these faster than I can work through them. Give it a minute."
)

APPROVE_EMOJI = "✅"
DENY_EMOJI = "❌"

HELD_TEXT = (
    "Logged with the change board — I'll come back to you on this one shortly."
)

# The built-in denial wording. The live value is ``bot.denied_text``: fixed
# per bot and editable by the white cell, never derived from the ruling, so
# a denial can carry nothing about what the change would have touched.
HITL_DENIED_TEXT = DEFAULT_HITL_DENIED_TEXT

_MENTION = re.compile(r"<@!?\d+>")

# The white cell channel is the spec's sole mechanism for catching a bad
# ruling mid-exercise. When it's unreachable, rulings still go out publicly
# with zero oversight and the only prior signal was a traceback nobody
# watches. This has to be impossible to miss on the console at startup, the
# one moment an operator is actually looking -- and it has to say *why*, so
# the operator fixes the right thing instead of guessing between a wrong ID,
# a private channel, and a missing permission.
_WHITE_CELL_BANNER = """
================================================================
 WHITE CELL CHANNEL UNUSABLE (channel_id=%s)
 Problem: %s
 Oversight posting is NOT working: rulings will still be answered
 publicly, but no one is reviewing them until this is fixed.
 Fix the channel or the bot's permissions, then restart.
================================================================"""

NOT_ALLOWED_HERE_TEXT = (
    "RangeControl isn't enabled in this channel. Ask in one of the "
    "channels set up for change requests."
)


def should_handle(channel_id: int, allowed: tuple[int, ...]) -> bool:
    """An empty allowlist means every channel is permitted."""
    return not allowed or channel_id in allowed


class BotLike(Protocol):
    """What ``handle_question`` needs from a bot.

    Declared so the pipeline can be driven by a test double without dragging
    ``discord.Client.__init__`` into every test.
    """

    advisor: Advisor
    audit: AuditLog
    limiter: RateLimiter
    mode: ModeSwitch
    pending: PendingRegistry
    denied_text: str

    async def post_white_cell(
        self,
        advice: Advice,
        *,
        user_id: int,
        user_name: str,
        channel_id: int,
        channel_name: str,
        question: str,
        public_text: str,
        hitl_enabled: bool,
    ) -> int | None: ...

    async def send_to_channel(self, channel_id: int, text: str) -> None: ...

    def report_error(self, text: str) -> None: ...


async def handle_question(
    bot: BotLike,
    *,
    question: str,
    user_id: int,
    user_name: str,
    channel_id: int,
    channel_name: str,
) -> str:
    """Run one question end to end and return the public reply."""
    if not bot.limiter.allow(user_id):
        # The spec requires every interaction be recorded, and denial probing
        # is exactly the behavior most likely to trip the limiter — so
        # without this, the probing attempts are precisely what would be
        # missing from the audit trail.
        bot.audit.record(
            Advice(kind="rate_limited", public_text=RATE_LIMIT_TEXT),
            user_id=user_id,
            user_name=user_name,
            channel_id=channel_id,
            question=question,
            latency_ms=0,
        )
        return RATE_LIMIT_TEXT

    # The provider SDKs are synchronous and a ruling over a large range can
    # take tens of seconds. On the event loop thread that would stall the
    # gateway heartbeat (discord.py drops the connection after a long enough
    # stall) and queue every other question and reaction behind it.
    started = time.monotonic()
    advice = await asyncio.to_thread(bot.advisor.advise, question)
    latency_ms = int((time.monotonic() - started) * 1000)

    public_text = format_public(advice)

    # Read the switch exactly once. ModeSwitch exists precisely so the GUI
    # checkbox can flip it mid-exercise, and reading it a second time later in
    # this function could observe a different value than this one — producing
    # a held request with no reactions on it, or an answered ruling whose
    # white cell embed still says "AWAITING REVIEW". Everything downstream
    # (the embed, the hold decision) is derived from this one read.
    hitl_enabled = bot.mode.enabled
    should_hold = hitl_enabled and advice.kind == "ruling"

    approval_message_id: int | None = None
    try:
        approval_message_id = await bot.post_white_cell(
            advice,
            user_id=user_id,
            user_name=user_name,
            channel_id=channel_id,
            channel_name=channel_name,
            question=question,
            public_text=public_text,
            hitl_enabled=hitl_enabled,
        )
    except Exception:  # noqa: BLE001 - a broken audit channel must not block the reply
        logger.exception(
            "failed to post to the white cell channel "
            "(asker=%s, channel=%s, question=%r)",
            user_name,
            channel_name,
            question,
        )

    # Only a ruling carries an adjudication worth reviewing; deflections,
    # clarifications, rate-limit notices, and errors go out immediately. And
    # if the white cell post failed, there is nobody to review it — holding
    # the request would block the asker forever with no one aware, so it is
    # answered exactly as it would be with the mode off. When it succeeded,
    # ``bot.post_white_cell`` has already registered the hold in
    # ``bot.pending`` (before adding its reactions — see its docstring).
    held = should_hold and approval_message_id is not None

    # Recorded after the hold decision, not before: advice.public_text is the
    # model's ruling text, and a record written earlier had no way to say
    # that text was never actually sent — an after-action review would read
    # a delivered approval that was, in fact, being held for white cell
    # sign-off. latency_ms is still the ruling computation alone, captured
    # above before post_white_cell's own round trip.
    bot.audit.record(
        advice,
        user_id=user_id,
        user_name=user_name,
        channel_id=channel_id,
        question=question,
        latency_ms=latency_ms,
        held=held,
    )

    if should_hold:
        if held:
            return HELD_TEXT

        logger.warning(
            "human-in-the-loop is on but the white cell post failed; "
            "answering %s directly with no review (channel=%s, question=%r)",
            user_name,
            channel_name,
            question,
        )
        bot.report_error(
            f"White cell post failed while human-in-the-loop is on; "
            f"answered {user_name} directly with no review (channel="
            f"{channel_name})."
        )

    return public_text


async def release_pending(
    bot: BotLike, *, approval_message_id: int, emoji: str, reviewer: str = ""
) -> None:
    """React to a held ruling: release it on approval, refuse it on denial.

    Any other emoji is ignored. ``take`` removes the request from the
    registry before anything else happens, so a second reaction — from the
    same or a different reviewer — finds nothing to release.
    """
    if emoji not in (APPROVE_EMOJI, DENY_EMOJI):
        return

    request = bot.pending.take(approval_message_id)
    if request is None:
        return

    # Prefixed with a mention: with several blue teamers sharing one
    # channel, an unaddressed release or denial is ambiguous -- two denials
    # read byte-identical, and nobody knows which question either answers.
    # A mention carries no range knowledge, so it costs nothing to add.
    #
    # request.public_text already went through format_public() once, in
    # handle_question, so it can already sit at exactly DISCORD_LIMIT --
    # prepending the mention here without re-truncating would then push a
    # near-maximal ruling past Discord's own ceiling. channel.send would
    # raise, the except below would log and swallow it, and the request
    # (already taken out of pending above) could never be retried: the
    # asker gets no answer at all while the audit says "hitl_approved".
    # Re-truncating the composed string, not just the ruling alone, is what
    # closes that gap.
    mention = f"<@{request.user_id}>"
    if emoji == APPROVE_EMOJI:
        text = _truncate(f"{mention} {request.public_text}", DISCORD_LIMIT)
        kind = "hitl_approved"
    else:
        denied = bot.denied_text.strip() or HITL_DENIED_TEXT
        text = _truncate(f"{mention} {denied}", DISCORD_LIMIT)
        kind = "hitl_denied"

    # The audit entry must exist even when delivery fails. A contested
    # denial that never reached the asker is exactly the case an
    # after-action review needs the trail for most, and the request has
    # already been taken out of ``pending`` — losing the audit line here
    # would lose the only remaining record the decision was ever made.
    # Delivery failure is logged separately and never allowed to escape
    # the reaction handler that calls this.
    try:
        await bot.send_to_channel(request.channel_id, text)
    except Exception:  # noqa: BLE001 - a broken channel must not swallow the audit trail
        logger.exception(
            "failed to deliver the %s decision to channel %s "
            "(asker=%s, question=%r)",
            kind,
            request.channel_id,
            request.user_name,
            request.question,
        )

    bot.audit.record(
        Advice(kind=kind, public_text=text),
        user_id=request.user_id,
        user_name=request.user_name,
        channel_id=request.channel_id,
        question=request.question,
        latency_ms=0,
        reviewer=reviewer,
    )


class RangeControlBot(discord.Client):
    def __init__(
        self,
        config: Config,
        advisor: Advisor,
        audit: AuditLog,
        limiter: RateLimiter | None = None,
        on_pending_change: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.config = config
        self.advisor = advisor
        self.audit = audit
        self.limiter = limiter or RateLimiter()
        self.mode = ModeSwitch(config.human_in_the_loop)
        # A plain attribute, reassigned whole from the GUI thread: a str
        # swap is atomic under the GIL, and release_pending reads it once.
        self.denied_text = config.hitl_denied_text
        self.pending = PendingRegistry(on_change=on_pending_change)
        self._on_error = on_error
        self._channels_checked = False
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def report_error(self, text: str) -> None:
        """Surface a condition the GUI's status bar must show persistently.

        A no-op when nothing is listening (the CLI never passes on_error):
        the CLI path still gets the same information via ``logger``, which
        every caller of this already logs alongside calling it.
        """
        if self._on_error is not None:
            self._on_error(text)

    def _register_commands(self) -> None:
        @self.tree.command(name="rc", description="Ask whether a change is authorized")
        @app_commands.describe(question="The change you want to make")
        async def rc(interaction: discord.Interaction, question: str) -> None:
            await self.handle_slash(interaction, question)

    async def handle_slash(self, interaction, question: str) -> None:
        """Answer a /rc invocation.

        A channel outside the allowlist gets a private one-liner rather than
        silence: an unanswered interaction makes Discord display "The
        application did not respond", which looks like an outage to the
        blue team and generates support traffic for the white cell.
        """
        if not should_handle(interaction.channel_id, self.config.allowed_channel_ids):
            await interaction.response.send_message(
                NOT_ALLOWED_HERE_TEXT, ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        reply = await handle_question(
            self,
            question=question,
            user_id=interaction.user.id,
            user_name=str(interaction.user),
            channel_id=interaction.channel_id,
            channel_name=getattr(interaction.channel, "name", "unknown"),
        )
        await interaction.followup.send(reply)

    async def setup_hook(self) -> None:
        # Command definitions do not change between restarts, so a failed sync
        # is not worth refusing to start over -- Discord rate-limits application
        # command syncs, and a crash-looping process would hit that ceiling
        # exactly when the bot is already unstable. Mentions keep working, and
        # previously-registered slash commands stay registered.
        try:
            await self.tree.sync()
        except Exception:  # noqa: BLE001 - never block startup on a sync failure
            logger.exception("slash command sync failed; continuing")

    async def on_ready(self) -> None:
        # READY fires again after every gateway resume; the channels have not
        # changed, and re-posting the banner would bury real log lines.
        if self._channels_checked:
            return
        self._channels_checked = True
        await self.verify_channels()

    async def verify_channels(self) -> None:
        """Check every configured channel once, while an operator is watching.

        Runs after READY rather than in ``setup_hook`` so that ``guild.me``
        exists and the bot's effective permissions can be computed. A wrong
        ID, a private channel the bot is not in, and a missing Embed Links
        permission all used to surface as the same "unreachable" banner, or
        not at all until the first ruling failed. Never refuses to start: a
        bot serving the exercise without oversight beats no bot mid-exercise.
        """
        for channel_id in self.config.allowed_channel_ids:
            check = await check_channel(self, channel_id, ALLOWED_CHANNEL_PERMISSIONS)
            if check.ok:
                logger.info("Allowed channel %s verified.", check.describe())
                continue
            text = (
                f"Allowed channel {check.describe()} is unusable: {check.problem} "
                "Questions asked there will be ignored."
            )
            logger.warning(text)
            self.report_error(text)

        white = await check_channel(
            self, self.config.white_cell_channel_id, WHITE_CELL_PERMISSIONS
        )
        if white.ok:
            suffix = f" ({white.note})" if white.note else ""
            logger.info(
                "White cell channel %s verified; audit embeds and approval "
                "reactions will post there.%s",
                white.describe(),
                suffix,
            )
            return
        self._report_white_cell_problem(white)

    def _report_white_cell_problem(self, check: ChannelCheck) -> None:
        logger.error(_WHITE_CELL_BANNER, check.channel_id, check.problem)
        # The design spec calls for this to become "a persistent red bar":
        # a logger.error alone lands only in ConsoleTab.append_log, a line
        # that scrolls away with everything else. This is the actual wire to
        # the GUI's status bar (see gui/events.py's ERROR kind and
        # StatusBar.set_warning).
        self.report_error(
            f"White cell channel {check.describe()} is unusable: {check.problem} "
            "Rulings are still being answered publicly with no oversight "
            "until this is fixed."
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user not in message.mentions:
            return
        if not should_handle(message.channel.id, self.config.allowed_channel_ids):
            return

        question = _MENTION.sub("", message.content).strip()
        async with message.channel.typing():
            reply = await handle_question(
                self,
                question=question,
                user_id=message.author.id,
                user_name=str(message.author),
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", "unknown"),
            )
        await message.reply(reply, mention_author=False)

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        # Only the white cell channel can release a hold, and only a human
        # can: the bot's own reactions (added alongside the prompt) must
        # never be mistaken for a reviewer's decision, and neither can any
        # other bot's. Fail closed on an unset ``self.user``: treating "we
        # don't know who we are yet" as "definitely not me" would be a
        # security boundary quietly failing open.
        if payload.channel_id != self.config.white_cell_channel_id:
            return
        if self.user is None or payload.user_id == self.user.id:
            return

        reviewer = ""
        member = payload.member
        if member is not None:
            if member.bot:
                return
            reviewer = member.display_name
        else:
            user = self.get_user(payload.user_id)
            if user is not None:
                if getattr(user, "bot", False):
                    return
                reviewer = str(user)

        await release_pending(
            self,
            approval_message_id=payload.message_id,
            emoji=str(payload.emoji),
            reviewer=reviewer,
        )

    async def send_to_channel(self, channel_id: int, text: str) -> None:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        await channel.send(text)

    async def post_white_cell(
        self,
        advice: Advice,
        *,
        user_id: int,
        user_name: str,
        channel_id: int,
        channel_name: str,
        question: str,
        public_text: str,
        hitl_enabled: bool,
    ) -> int | None:
        """Post the audit embed, and hold a ruling for review when asked to.

        ``hitl_enabled`` is passed in rather than read from ``self.mode``
        again: it must be the exact same value ``handle_question`` already
        used to decide whether to return ``HELD_TEXT``, not a fresh read that
        could observe a mode flip that happened in between.
        """
        channel = self.get_channel(self.config.white_cell_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.white_cell_channel_id)

        awaiting = hitl_enabled and advice.kind == "ruling"
        message = await channel.send(
            embed=build_embed(
                advice,
                user_name=user_name,
                channel_name=channel_name,
                question=question,
                awaiting=awaiting,
                hitl_enabled=hitl_enabled,
            )
        )

        if awaiting:
            # Register the hold before the reactions go up, not after. A
            # reviewer who reacts in the gap between the message existing and
            # the reactions appearing on it would otherwise find nothing to
            # release — and Discord does not re-dispatch the same user+emoji,
            # so that reaction is gone for good and the request strands with
            # nobody aware.
            self.pending.add(
                message.id,
                PendingRequest(
                    channel_id=channel_id,
                    message_id=message.id,
                    user_id=user_id,
                    user_name=user_name,
                    question=question,
                    public_text=public_text,
                ),
            )
            try:
                await message.add_reaction(APPROVE_EMOJI)
                await message.add_reaction(DENY_EMOJI)
            except Exception:  # noqa: BLE001 - a missing permission must not undo the hold
                logger.exception(
                    "failed to add approval reactions to the white cell "
                    "message (message_id=%s); the ruling is held but "
                    "nothing can release it until this is fixed",
                    message.id,
                )

        return message.id
