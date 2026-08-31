"""`/admin` -- the only admin-facing slash command, plus the restock-alert
background scan that posts to `ALERTS_CHANNEL_ID`.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import alerts

from .. import layout
from ..permissions import is_staff
from ..views.admin import AdminPanelView, build_admin_embed
from ..views.alerts import AlertAckView, build_alert_embed


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.scan_alerts.start()

    def cog_unload(self) -> None:
        self.scan_alerts.cancel()

    @app_commands.command(name="admin", description="Items, prices, thresholds, markets, treasury.")
    async def admin(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = getattr(self.bot, "nola_config", None)
        if config is None or not is_staff(interaction.user, config):
            await interaction.followup.send("This panel is staff-only.", ephemeral=True)
            return
        embed = build_admin_embed(config)
        await interaction.followup.send(
            embed=embed, view=AdminPanelView(interaction.user.id, config), ephemeral=True
        )

    @tasks.loop(minutes=10)
    async def scan_alerts(self) -> None:
        config = getattr(self.bot, "nola_config", None)
        if config is None:
            return
        # Through the layout, so this keeps working on a server that was set
        # up by /setup and never had an ALERTS_CHANNEL_ID. None is normal
        # before setup has run -- returning quietly is right, because raising
        # inside a task loop sends the traceback somewhere nobody reads.
        channel = layout.channel(self.bot, config, "channel:alerts")
        if channel is None:
            return
        for due_row in alerts.due():
            await channel.send(embed=build_alert_embed(due_row), view=AlertAckView())

    @scan_alerts.before_loop
    async def before_scan(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
