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

    @app_commands.command(name="casino", description="Coinflip and dice, provably fair.")
    async def casino(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = getattr(self.bot, "nola_config", None)
        currency_name = config.currency_name if config else "coin"
        embed = panel_embed(
            "Casino",
            "Coinflip and dice. Every round is provably fair -- verify any past round below.",
        )
        await interaction.followup.send(
            embed=embed, view=CasinoPanelView(interaction.user.id, currency_name), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
