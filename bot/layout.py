"""Resolving a channel the bot needs, wherever its id happens to live.

Two sources, in this order:

  1. the environment (`SHOP_CHANNEL_ID` and friends) -- an override, kept for
     a server that was wired by hand before `/setup` existed
  2. `guild_layout` -- what `/setup` built

Environment first, deliberately: it matches how `core.env` treats a real
variable, and it means someone can always pin a channel by hand without
fighting the table.

Returning None is a normal answer on a server where `/setup` has not run. Every
caller must handle it -- a missing alerts channel must never be the reason a
restock scan raises inside a background task, where the traceback goes nowhere
anyone will read it.
"""
from __future__ import annotations

from typing import Optional

import discord

from core import provision
from core.config import BotConfig

# our layout key -> the env var that overrides it
ENV_OVERRIDES: dict[str, str] = {
    "channel:shop": "shop_channel_id",
    "channel:orders": "orders_channel_id",
    "channel:alerts": "alerts_channel_id",
}


def channel_id_for(config: BotConfig, key: str) -> Optional[int]:
    attr = ENV_OVERRIDES.get(key)
    if attr:
        from_env = getattr(config, attr, None)
        if from_env:
            return int(from_env)
    return provision.channel_id(config.guild_id, key)


def channel(bot: discord.Client, config: BotConfig, key: str):
    """The live channel object, or None if it is unset or no longer exists.

    A stored id that no longer resolves is left in the table rather than
    cleaned up here: deciding a channel is gone on the strength of one
    `get_channel` miss would drop the mapping during an ordinary cache gap.
    `/setup` is where stale rows get repointed, with the guild in hand.
    """
    cid = channel_id_for(config, key)
    if not cid:
        return None
    return bot.get_channel(int(cid))
