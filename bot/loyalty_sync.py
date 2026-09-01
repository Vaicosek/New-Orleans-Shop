"""Keeps a subject's loyalty-rank Discord role matched to core/loyalty.py's
computed (or staff-overridden) tier.

Pure bot-layer glue, same seam as core/provision.py's own docstring
describes: core/loyalty.py knows nothing about Discord, this module knows
nothing about points math -- it reads `loyalty.effective_tier()` and moves
roles to match. Every call is best-effort: a role sync that fails (member
left, bot lacks Manage Roles, its top role sits below the rank roles in the
hierarchy) is logged and swallowed, never raised, because a stale role is
cosmetic and must never be the reason an order payout or an auction
settlement itself fails.
"""
from __future__ import annotations

from typing import Iterable

import discord

from core import loyalty, provision

#: role_key -> tier key, for every rank role /setup can build.
_ROLE_KEYS = {f"role:rank:{t['key']}": t["key"] for t in loyalty.TIERS}


def _discord_id(subject: str) -> int | None:
    if not subject.startswith("u:"):
        return None
    raw = subject.split(":", 1)[1]
    return int(raw) if raw.isdigit() else None


async def sync_rank_role(bot: discord.Client, guild_id: int, subject: str) -> None:
    """Move `subject`'s Discord roles to match their current tier, adding
    the right one and removing every other rank role they hold. Silently
    does nothing if the guild's layout has no rank roles yet (run /setup),
    the subject isn't a Discord user, or they aren't a member of the guild.
    """
    discord_id = _discord_id(subject)
    if discord_id is None:
        return

    stored = provision.stored(guild_id)
    role_ids = {key: stored.get(key) for key in _ROLE_KEYS}
    if not any(role_ids.values()):
        return  # /setup hasn't built the rank roles yet

    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except discord.HTTPException:
            return

    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except discord.HTTPException:
            return  # not a member here (or the bot can't see them) -- nothing to sync

    tier = loyalty.effective_tier(subject)
    desired_role_id = role_ids.get(f"role:rank:{tier['key']}")

    held_rank_role_ids = {
        role.id for role in member.roles if role.id in set(filter(None, role_ids.values()))
    }
    to_remove = [
        guild.get_role(rid) for rid in held_rank_role_ids
        if rid != desired_role_id and guild.get_role(rid) is not None
    ]
    to_add = (
        guild.get_role(desired_role_id)
        if desired_role_id is not None and desired_role_id not in held_rank_role_ids
        else None
    )

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="loyalty rank sync")
        if to_add is not None:
            await member.add_roles(to_add, reason="loyalty rank sync")
    except discord.Forbidden as err:
        print(f"[loyalty] can't sync rank role for {subject}: {err!r} "
              f"-- check the bot's role sits above the rank roles", flush=True)
    except discord.HTTPException as err:
        print(f"[loyalty] rank role sync failed for {subject}: {err!r}", flush=True)


async def sync_rank_roles(bot: discord.Client, guild_id: int, subjects: Iterable[str]) -> None:
    """Convenience for syncing several subjects one after another -- an
    order can pay out several workers in one approval."""
    for subject in subjects:
        await sync_rank_role(bot, guild_id, subject)
