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


async def test_reachable_channel_with_all_permissions_is_ok():
    result = await check_channel(_bot(_channel()), 42, WHITE_CELL_PERMISSIONS)
    assert result.ok
    assert result.name == "white-cell"
    assert result.problem == ""


async def test_wrong_id_names_the_id_as_the_problem():
    bot = _bot(fetch_error=_http_error(discord.NotFound, 404, "Not Found"))
    result = await check_channel(bot, 42, WHITE_CELL_PERMISSIONS)
    assert not result.ok
    assert "channel ID" in result.problem
    assert "invited" in result.problem


async def test_private_channel_the_bot_cannot_see_says_so():
    bot = _bot(fetch_error=_http_error(discord.Forbidden, 403, "Forbidden"))
    result = await check_channel(bot, 42, WHITE_CELL_PERMISSIONS)
    assert not result.ok
    assert "cannot see" in result.problem
    assert "role" in result.problem


async def test_missing_permissions_are_listed_by_name():
    channel = _channel(perms=_perms(embed_links=False, add_reactions=False))
    result = await check_channel(_bot(channel), 42, WHITE_CELL_PERMISSIONS)
    assert not result.ok
    assert "Embed Links" in result.problem
    assert "Add Reactions" in result.problem
    assert "Send Messages" not in result.problem


async def test_allowed_channels_need_fewer_permissions():
    channel = _channel(perms=_perms(embed_links=False, add_reactions=False))
    result = await check_channel(_bot(channel), 42, ALLOWED_CHANNEL_PERMISSIONS)
    assert result.ok


async def test_a_direct_message_channel_is_rejected():
    result = await check_channel(_bot(_channel(guild=False)), 42, WHITE_CELL_PERMISSIONS)
    assert not result.ok
    assert "server" in result.problem


async def test_unexpected_failure_reports_the_exception():
    bot = _bot(fetch_error=RuntimeError("socket closed"))
    result = await check_channel(bot, 42, WHITE_CELL_PERMISSIONS)
    assert not result.ok
    assert "RuntimeError" in result.problem
    assert "socket closed" in result.problem


async def test_cache_miss_falls_back_to_the_api():
    channel = _channel()
    bot = _bot(None)
    bot.fetch_channel = AsyncMock(return_value=channel)
    result = await check_channel(bot, 42, WHITE_CELL_PERMISSIONS)
    assert result.ok
    bot.fetch_channel.assert_awaited_once_with(42)


async def test_permissions_cannot_be_checked_without_a_member_object():
    """Before the member cache is populated guild.me is None; that must not
    crash the check or be mistaken for a permission failure."""
    result = await check_channel(_bot(_channel(me=None)), 42, WHITE_CELL_PERMISSIONS)
    assert result.ok
    assert "could not be verified" in result.note
