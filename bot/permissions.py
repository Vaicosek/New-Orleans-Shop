"""Staff / manager role checks. One place, so a panel never hand-rolls its
own notion of who is allowed to touch the treasury.

Uses the interacting member's live roles -- never a cached id list on a view,
since a view can outlive the moment its owner's roles were read.
"""
from __future__ import annotations

import discord

from core import provision
from core.config import BotConfig


def _role_ids(member: discord.abc.User | discord.Member) -> set[int]:
    roles = getattr(member, "roles", None)
    if not roles:
        return set()
    return {r.id for r in roles}


def _provisioned(member: discord.abc.User | discord.Member, key: str) -> set[int]:
    """The role `/setup` built for `key`, if any.

    Without this, `/setup` is a trap: it creates the Staff and Manager roles,
    hands them out, and NOBODY gains any permission -- because these checks
    read STAFF_ROLE_IDS from the environment, which is empty on a server that
    was set up rather than hand-wired. That is the same chicken-and-egg the
    channel ids had, and it was fixed for channels and missed for roles.

    Env still wins when set: an operator who pinned role ids by hand gets
    exactly those, and the provisioned roles fill the gap otherwise.
    """
    guild = getattr(member, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        return set()
    role_id = provision.channel_id(int(guild_id), key)
    return {int(role_id)} if role_id else set()


def is_staff(member: discord.abc.User | discord.Member, config: BotConfig) -> bool:
    allowed = set(config.staff_role_ids) or _provisioned(member, "role:staff")
    if not allowed:
        return False
    return bool(_role_ids(member) & allowed)


def is_manager(member: discord.abc.User | discord.Member, config: BotConfig) -> bool:
    allowed = set(config.manager_role_ids) or _provisioned(member, "role:manager")
    if allowed and (_role_ids(member) & allowed):
        return True
    return is_staff(member, config)


def is_owner(member: discord.abc.User | discord.Member, config: BotConfig) -> bool:
    """Owners only: funding a treasury creates coins that did not exist.

    Checked against a USER id list rather than a role, because a role can be
    granted by anyone who can manage roles -- which would make "who may mint"
    a Discord permissions question instead of an explicit list.
    """
    return int(getattr(member, "id", 0)) in set(config.owner_discord_ids)
