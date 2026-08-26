"""`/predict` -- the only prediction-market-facing slash command."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..views.predict import PredictPanelView, build_markets_embed


class PredictCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="predict", description="Open prediction markets, stake, your positions.")
    async def predict(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = build_markets_embed()
        await interaction.followup.send(
            embed=embed, view=PredictPanelView(interaction.user.id), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PredictCog(bot))
