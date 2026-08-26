"""Staff / manager role checks. One place, so a panel never hand-rolls its
own notion of who is allowed to touch the treasury.

Uses the interacting member's live roles -- never a cached id list on a view,
since a view can outlive the moment its owner's roles were read.
"""
from __future__ import annotations

import discord

from core.config import BotConfig


def _role_ids(member: discord.abc.User | discord.Member) -> set[int]:
    roles = getattr(member, "roles", None)
    if not roles:
        return set()
    return {r.id for r in roles}


def is_staff(member: discord.abc.User | discord.Member, config: BotConfig) -> bool:
    if not config.staff_role_ids:
        return False
    return bool(_role_ids(member) & set(config.staff_role_ids))


def is_manager(member: discord.abc.User | discord.Member, config: BotConfig) -> bool:
    if not config.manager_role_ids:
        return False
    return bool(_role_ids(member) & set(config.manager_role_ids)) or is_staff(member, config)
