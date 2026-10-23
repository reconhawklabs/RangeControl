"""Startup channel diagnostics: the bot must say exactly why a channel is unusable.

The white cell channel is the only oversight mechanism. A check that reports
"unreachable" without saying whether the ID is wrong, the channel is private,
or a permission is missing leaves the operator guessing mid-exercise.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from rangecontrol.bot.diagnostics import (
    ALLOWED_CHANNEL_PERMISSIONS,
    WHITE_CELL_PERMISSIONS,
    check_channel,
)


def _http_error(cls, status, reason):
    return cls(SimpleNamespace(status=status, reason=reason), "boom")


def _perms(**overrides):
    granted = {name: True for name in WHITE_CELL_PERMISSIONS}
    granted.update(overrides)
    return SimpleNamespace(**granted)


def _channel(name="white-cell", *, me=object(), perms=None, guild=True):
    perms = perms or _perms()
    guild_obj = SimpleNamespace(name="Range", me=me) if guild else None
    return SimpleNamespace(
        id=42,
        name=name,
        guild=guild_obj,
        permissions_for=lambda member: perms,
    )


def _bot(channel=None, fetch_error=None):
    bot = SimpleNamespace()
    bot.get_channel = lambda channel_id: channel
    bot.fetch_channel = AsyncMock(
        side_effect=fetch_error, return_value=channel
    )
    return bot


async def test_a_usable_channel_missing_from_the_cache_is_fetched_and_passes():
    bot = _bot(None)
    bot.fetch_channel = AsyncMock(return_value=_channel())

    result = await check_channel(bot, 42, WHITE_CELL_PERMISSIONS)

    assert result.ok
    assert result.name == "white-cell"
    assert result.problem == ""
    bot.fetch_channel.assert_awaited_once_with(42)


async def test_each_unreachable_cause_is_named():
    cases = [
        ("wrong ID", _bot(fetch_error=_http_error(discord.NotFound, 404, "Not Found")),
         ("channel ID", "invited")),
        ("private channel", _bot(fetch_error=_http_error(discord.Forbidden, 403, "Forbidden")),
         ("cannot see", "role")),
        ("direct message", _bot(_channel(guild=False)), ("server",)),
        ("unexpected failure", _bot(fetch_error=RuntimeError("socket closed")),
         ("RuntimeError", "socket closed")),
    ]
    for label, bot, expected in cases:
        result = await check_channel(bot, 42, WHITE_CELL_PERMISSIONS)
        assert not result.ok, label
        for phrase in expected:
            assert phrase in result.problem, (label, phrase)


async def test_missing_permissions_are_named_against_what_the_channel_needs():
    """The white cell needs Embed Links and Add Reactions; an allowed channel,
    which only reads mentions and replies, does not."""
    channel = _channel(perms=_perms(embed_links=False, add_reactions=False))

    white = await check_channel(_bot(channel), 42, WHITE_CELL_PERMISSIONS)
    assert not white.ok
    assert "Embed Links" in white.problem
    assert "Add Reactions" in white.problem
    assert "Send Messages" not in white.problem

    allowed = await check_channel(_bot(channel), 42, ALLOWED_CHANNEL_PERMISSIONS)
    assert allowed.ok


async def test_permissions_cannot_be_checked_without_a_member_object():
    """Before the member cache is populated guild.me is None; that must not
    crash the check or be mistaken for a permission failure."""
    result = await check_channel(_bot(_channel(me=None)), 42, WHITE_CELL_PERMISSIONS)
    assert result.ok
    assert "could not be verified" in result.note
