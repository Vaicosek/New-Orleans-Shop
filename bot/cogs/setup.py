"""`/setup` -- build the server layout.

Its own command rather than a button inside `/admin`, for a reason that is
not cosmetic: `/admin` is gated on `is_staff`, which reads STAFF_ROLE_IDS, and
on a fresh server that list is empty -- so `is_staff` is False for everyone,
including the person who owns the server. `/admin` is therefore unusable until
setup has run, and a setup button living inside it could never be pressed.

Gated on the owner list, and on the guild's actual owner as a fallback for the
same reason: OWNER_DISCORD_IDS may not be filled in yet either.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..permissions import is_owner
from ..views.setup import SetupConfirmView, build_setup_embed, plan_for


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Build the New Orleans channels and roles.")
    @app_commands.guild_only()
    async def setup_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = getattr(self.bot, "nola_config", None)
        guild = interaction.guild
        if config is None or guild is None:
            await interaction.followup.send("Run this inside the server.", ephemeral=True)
            return

        allowed = is_owner(interaction.user, config) or interaction.user.id == guild.owner_id
        if not allowed:
            await interaction.followup.send(
                "Only the server owner can build the layout.", ephemeral=True
            )
            return

        steps = plan_for(guild)
        await interaction.followup.send(
            embed=build_setup_embed(steps, guild),
            view=SetupConfirmView(interaction.user.id, config),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
