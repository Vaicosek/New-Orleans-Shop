"""`/orders` -- the only orders-facing slash command."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..views.orders import OrdersPanelView, build_panel_embed


class OrdersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="orders", description="View the order board, claim, deliver, approve.")
    async def orders_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = getattr(self.bot, "nola_config", None)
        embed = build_panel_embed()
        await interaction.followup.send(
            embed=embed, view=OrdersPanelView(interaction.user.id, config), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OrdersCog(bot))
