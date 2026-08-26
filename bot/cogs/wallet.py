"""`/wallet` -- the only wallet-facing slash command."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core import money

from ..views.wallet import WalletPanelView, build_wallet_embed


class WalletCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="wallet", description="Balance, held gold, history, and transfer.")
    async def wallet(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        subject = money.user(interaction.user.id)
        money.ensure_wallet(subject)
        embed = build_wallet_embed(subject)
        await interaction.followup.send(
            embed=embed, view=WalletPanelView(interaction.user.id), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WalletCog(bot))
