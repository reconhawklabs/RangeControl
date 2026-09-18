"""Startup checks that say exactly why a Discord channel cannot be used.

Run once the gateway is ready, not from ``setup_hook``: before the READY
event the channel cache is empty and ``guild.me`` is None, so the only thing
an early check can establish is whether the channel exists. Whether the bot
can actually post an embed and add reactions there is only knowable once the
member cache is populated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import discord

# What the white cell channel needs: an embed per ruling, plus the two
# reactions that release or deny a held ruling.
WHITE_CELL_PERMISSIONS: tuple[str, ...] = (
    "view_channel",
    "send_messages",
    "embed_links",
    "add_reactions",
    "read_message_history",
)

# What a channel the blue team asks in needs: read the mention, reply to it.
ALLOWED_CHANNEL_PERMISSIONS: tuple[str, ...] = (
    "view_channel",
    "send_messages",
    "read_message_history",
)

_PERMISSION_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "add_reactions": "Add Reactions",
    "read_message_history": "Read Message History",
}

NOT_FOUND_TEXT = (
    "Discord has no channel with this ID that the bot can reach. Check the "
    "channel ID (Developer Mode -> right-click the channel -> Copy Channel "
    "ID) and that the bot has been invited to that server."
)

FORBIDDEN_TEXT = (
    "the bot cannot see this channel. It is private, so give the bot a role "
    "that has View Channel there, or add the bot itself under the channel's "
    "permissions."
)

NOT_A_GUILD_CHANNEL_TEXT = (
    "this is not a server text channel. Use a text channel in the exercise "
    "server, not a DM or group."
)

PERMISSIONS_UNVERIFIED_NOTE = (
    "permissions could not be verified yet (member cache not populated)"
)


class ChannelSource(Protocol):
    """The two lookups a discord.Client offers for a channel ID."""

    def get_channel(self, channel_id: int, /): ...

    async def fetch_channel(self, channel_id: int, /): ...


@dataclass(frozen=True)
class ChannelCheck:
    channel_id: int
    ok: bool
    name: str = ""
    problem: str = ""
    note: str = ""

    def describe(self) -> str:
        """One line naming the channel, for logs and the status bar."""
        label = f"#{self.name}" if self.name else f"channel {self.channel_id}"
        return f"{label} ({self.channel_id})"


def _labels(names: list[str]) -> str:
    return ", ".join(_PERMISSION_LABELS.get(name, name) for name in names)


async def check_channel(
    bot: ChannelSource, channel_id: int, required: tuple[str, ...]
) -> ChannelCheck:
    """Resolve ``channel_id`` and confirm the bot holds ``required`` there.

    Never raises: every failure becomes a ``ChannelCheck`` whose ``problem``
    tells the operator what to change. The three common causes (wrong ID,
    private channel, missing permission) each get their own wording because
    each has a different fix.
    """
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
    except discord.NotFound:
        return ChannelCheck(channel_id, ok=False, problem=NOT_FOUND_TEXT)
    except discord.Forbidden:
        return ChannelCheck(channel_id, ok=False, problem=FORBIDDEN_TEXT)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never raise
        return ChannelCheck(
            channel_id,
            ok=False,
            problem=f"could not reach it ({type(exc).__name__}: {exc})",
        )

    name = str(getattr(channel, "name", "") or "")
    guild = getattr(channel, "guild", None)
    if guild is None:
        return ChannelCheck(
            channel_id, ok=False, name=name, problem=NOT_A_GUILD_CHANNEL_TEXT
        )

    me = getattr(guild, "me", None)
    if me is None:
        return ChannelCheck(
            channel_id, ok=True, name=name, note=PERMISSIONS_UNVERIFIED_NOTE
        )

    try:
        perms = channel.permissions_for(me)
        missing = [flag for flag in required if not getattr(perms, flag, False)]
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never raise
        return ChannelCheck(
            channel_id,
            ok=True,
            name=name,
            note=f"permissions could not be verified ({type(exc).__name__}: {exc})",
        )

    if missing:
        return ChannelCheck(
            channel_id,
            ok=False,
            name=name,
            problem=(
                f"the bot is missing these permissions there: {_labels(missing)}. "
                "Grant them on the channel or on the bot's role."
            ),
        )
    return ChannelCheck(channel_id, ok=True, name=name)
