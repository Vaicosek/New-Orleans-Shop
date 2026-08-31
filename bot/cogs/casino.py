"""`/casino` -- the only casino-facing slash command."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..ui.embed import panel_embed
from ..views.casino import CasinoPanelView


class CasinoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="casino", description="Coinflip, dice and slots, provably fair.")
    async def casino(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = panel_embed(
            "Casino",
            "Coinflip, dice and slots. Every round is provably fair · verify any past round below.",
        )
        await interaction.followup.send(
            embed=embed, view=CasinoPanelView(interaction.user.id), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
