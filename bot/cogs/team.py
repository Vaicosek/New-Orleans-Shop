"""`/team` -- the only team-facing slash command. See CONTRACT.md section 7."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core import money, teams as teams_core

from ..permissions import is_manager
from ..views.team import (
    TeamJoinView,
    TeamManagerView,
    TeamMemberView,
    build_no_team_embed,
    build_team_embed,
)


class TeamCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="team", description="Manager rosters -- run a team, or join one.")
    async def team(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        subject = money.user(interaction.user.id)
        money.ensure_wallet(subject)

        team = teams_core.team_of(subject)
        if team is not None and team["manager"] == subject:
            view: discord.ui.View = TeamManagerView(interaction.user.id, subject)
            embed = build_team_embed(team)
        elif team is not None:
            view = TeamMemberView(interaction.user.id, subject)
            embed = build_team_embed(team)
        else:
            config = getattr(self.bot, "nola_config", None)
            can_create = config is not None and is_manager(interaction.user, config)
            view = TeamJoinView(interaction.user.id, subject, can_create=can_create)
            embed = build_no_team_embed(can_create=can_create)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamCog(bot))
