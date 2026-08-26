"""`/shop` -- the only shop-facing slash command."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..views.shop import ShopPanelView, build_shop_embed


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="shop", description="Browse stock, look up prices, and open orders.")
    async def shop(self, interaction: discord.Interaction) -> None:
        # Defer inside 3 seconds -- catalog.search touches the database.
        await interaction.response.defer(ephemeral=True)
        config = getattr(self.bot, "nola_config", None)
        orders_channel_id = config.orders_channel_id if config else None
        embed = build_shop_embed()
        await interaction.followup.send(
            embed=embed, view=ShopPanelView(interaction.user.id, orders_channel_id), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopCog(bot))
