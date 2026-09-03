"""Team panel: `/team` -- manager-run rosters, worker join/leave.

Ported from AbexTech's `cogs/team.py` / `views/team_settings.py`, roster and
naming only. See `core/teams.py` and CONTRACT.md section 11d for what was
deliberately left out of the version this was adapted from: IGN linking (no
external sales feed here to attribute) and the manager override commission
on order payouts (a scope call, not a technical gap -- "just teams").

Four shapes for one command, chosen by what's true of the caller, never by
a sub-command: a manager who runs a team gets the roster panel; a manager
who doesn't yet gets a create-team prompt; a member gets their team's
roster read-only plus Leave; anyone else gets the list of teams to join.
"""
from __future__ import annotations

import discord

from core import catalog
from core import teams as teams_core

from .pickers import OptionPickerView, UserPickerView
from ..ui.embed import SEP, money_text, panel_embed, rows


def member_mention(subject: str) -> str:
    """Same rule as bot/views/land.py's `bidder_mention`."""
    if isinstance(subject, str) and subject.startswith("u:"):
        discord_id = subject.split(":", 1)[1]
        if discord_id.isdigit():
            return f"<@{discord_id}>"
    return subject


def _roster_lines(team_id: int) -> list[str]:
    members = teams_core.roster(team_id)
    if not members:
        return ["No members yet."]
    return [member_mention(s) for s in members]


def _focus_text(team_id: int) -> str:
    """A team with no focus rows works everything -- say that in words
    rather than showing an empty field, because "no categories" and "all
    categories" look identical otherwise and mean opposite things."""
    chosen = teams_core.focus(team_id)
    return ", ".join(chosen) if chosen else "everything"


def build_team_embed(team) -> discord.Embed:
    lines = [
        f"Manager: {member_mention(team['manager'])}",
        f"Works: {_focus_text(team['id'])}",
    ]
    standing = next((r for r in teams_core.leaderboard() if r["id"] == team["id"]), None)
    if standing is not None and standing["orders"]:
        lines.append(f"Paid to this team: {money_text(standing['paid'])} "
                     f"across {standing['orders']} order"
                     f"{'s' if standing['orders'] != 1 else ''}")
    lines.append("")
    lines.extend(_roster_lines(team["id"]))
    return panel_embed(team["name"], rows(lines))


def build_leaderboard_embed(*, this_month: bool = True) -> discord.Embed:
    """Teams by what their people have actually been paid. Ranked, because
    the point of teams here is that they compete -- and windowed to the
    month by default, because an all-time board freezes: whoever led in
    week one leads forever and nobody new bothers."""
    since = teams_core.month_start() if this_month else None
    board = teams_core.leaderboard(since=since)
    lines = []
    for i, row in enumerate(board, 1):
        size = f"{row['member_count'] + 1} strong"   # +1: the manager works too
        managed = f" (+{money_text(row['managed'])} managing)" if row["managed"] else ""
        lines.append(f"{i}. **{row['name']}** {SEP} {money_text(row['worked'])}{managed} {SEP} "
                     f"{row['orders']} order{'s' if row['orders'] != 1 else ''} {SEP} {size}")
    return panel_embed(
        "Team standings " + ("-- this month" if this_month else "-- all time"),
        rows(lines, empty_text="Nothing paid out yet this month." if this_month else "No teams yet."),
        footer="Ranked by gold actually paid for completed work",
    )


class StandingsView(discord.ui.View):
    """One toggle between the two windows, on the same message."""

    def __init__(self, owner_id: int, *, this_month: bool = True) -> None:
        super().__init__(timeout=180)
        self.owner_id, self.this_month = owner_id, this_month
        b = discord.ui.Button(
            label="Show all time" if this_month else "Show this month",
            style=discord.ButtonStyle.secondary,
        )
        b.callback = self._flip
        self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def _flip(self, interaction: discord.Interaction) -> None:
        flipped = not self.this_month
        await interaction.response.edit_message(
            embed=build_leaderboard_embed(this_month=flipped),
            view=StandingsView(self.owner_id, this_month=flipped),
        )


def build_no_team_embed(*, can_create: bool) -> discord.Embed:
    line = ("You don't run a team yet -- create one, or join one below."
            if can_create else "You're not on a team -- join one below.")
    return panel_embed("Teams", rows([line]))


def build_teams_list_embed() -> discord.Embed:
    all_teams = teams_core.list_teams()
    lines = [
        f"{t['name']} {SEP} run by {member_mention(t['manager'])} {SEP} "
        f"{t['member_count']} member{'s' if t['member_count'] != 1 else ''}"
        for t in all_teams
    ]
    return panel_embed("Teams", rows(lines, empty_text="No teams yet."))


class _TeamNameModal(discord.ui.Modal):
    """Shared by create and rename -- the only difference is which
    `core.teams` call `on_submit` makes, so one modal class covers both
    rather than two near-identical copies."""

    def __init__(self, title: str, on_submit_name):
        super().__init__(title=title, timeout=300)
        self._on_submit_name = on_submit_name
        self.name = discord.ui.TextInput(
            label="Team name", placeholder="e.g. The Levee Crew",
            max_length=40, required=True,
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_name(interaction, str(self.name.value).strip())


class TeamManagerView(discord.ui.View):
    """The roster panel a team's own manager sees: remove-by-select, add
    (UserPicker), rename, disband."""

    def __init__(self, owner_id: int, subject: str) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.subject = subject
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        team = teams_core.team_of(self.subject)
        if team is None or team["manager"] != self.subject:
            return
        members = teams_core.roster(team["id"])
        if members:
            options = [
                discord.SelectOption(label=member_mention(s)[:100] or s[:100], value=s[:100],
                                     description="remove from team")
                for s in members[:25]
            ]
            select: discord.ui.Select = discord.ui.Select(
                placeholder="Remove a member...", min_values=1, max_values=1, options=options,
            )

            async def _remove(interaction: discord.Interaction) -> None:
                teams_core.remove_member(self.subject, select.values[0])
                await self.refresh(interaction, "Removed.")

            select.callback = _remove
            self.add_item(select)

        for label, style, cb in (
            ("Add member", discord.ButtonStyle.primary, self._add),
            ("Set focus", discord.ButtonStyle.secondary, self._focus),
            ("Rename", discord.ButtonStyle.secondary, self._rename),
            ("Standings", discord.ButtonStyle.secondary, self._standings),
            ("Disband", discord.ButtonStyle.danger, self._disband),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = cb
            self.add_item(button)

    async def refresh(self, interaction: discord.Interaction, note: str = "") -> None:
        self._build()
        team = teams_core.team_of(self.subject)
        if team is None:
            embed = build_no_team_embed(can_create=True)
        else:
            embed = build_team_embed(team)
            if note:
                embed.description = (note + "\n\n" + (embed.description or "")).strip()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _add(self, interaction: discord.Interaction) -> None:
        async def picked(inter: discord.Interaction, member: discord.abc.User) -> None:
            if member.id == self.owner_id:
                await inter.response.send_message("You can't add yourself.", ephemeral=True)
                return
            try:
                teams_core.add_member(self.subject, f"u:{member.id}")
            except teams_core.TeamError as err:
                await inter.response.send_message(f"Couldn't add them: {err}", ephemeral=True)
                return
            await self.refresh(inter, f"Added {member.mention}.")

        await interaction.response.send_message(
            "Who are you adding?", view=UserPickerView(self.owner_id, picked), ephemeral=True,
        )

    async def _focus(self, interaction: discord.Interaction) -> None:
        """Which categories this team wants pinged for. A multi-select, so
        the manager sees every option and their current choice at once --
        never a typed list of category names."""
        team = teams_core.team_of(self.subject)
        if team is None:
            await interaction.response.send_message("You don't run a team.", ephemeral=True)
            return
        chosen = set(teams_core.focus(team["id"]))
        names = [c["name"] for c in catalog.list_categories()][:25]
        if not names:
            await interaction.response.send_message(
                "The catalog has no categories yet, so there is nothing to focus on.",
                ephemeral=True)
            return

        select: discord.ui.Select = discord.ui.Select(
            placeholder="Everything (choose to narrow)...",
            min_values=0, max_values=len(names),
            options=[discord.SelectOption(label=n[:100], value=n[:100],
                                          default=n in chosen) for n in names],
        )

        async def picked(inter: discord.Interaction) -> None:
            teams_core.set_focus(self.subject, list(select.values))
            await self.refresh(inter, f"Now working: {_focus_text(team['id'])}.")

        select.callback = picked
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message(
            "Pick the categories this team works. Choose none to work everything.",
            view=view, ephemeral=True)

    async def _standings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=build_leaderboard_embed(), view=StandingsView(interaction.user.id),
            ephemeral=True)

    async def _rename(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, name: str) -> None:
            teams_core.rename(self.subject, name)
            await self.refresh(inter, f"Renamed to **{name[:40] or 'Unnamed team'}**.")

        await interaction.response.send_modal(_TeamNameModal("Rename team", submitted))

    async def _disband(self, interaction: discord.Interaction) -> None:
        try:
            teams_core.disband(self.subject)
        except teams_core.TeamError as err:
            await interaction.response.send_message(f"Couldn't disband: {err}", ephemeral=True)
            return
        self._build()
        await interaction.response.edit_message(embed=build_no_team_embed(can_create=True), view=self)


class TeamMemberView(discord.ui.View):
    """Read-only roster plus Leave, for someone who is on a team but doesn't
    run it."""

    def __init__(self, owner_id: int, subject: str) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.subject = subject

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Standings", style=discord.ButtonStyle.secondary)
    async def standings(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=build_leaderboard_embed(), view=StandingsView(interaction.user.id),
            ephemeral=True)

    @discord.ui.button(label="Leave team", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        teams_core.leave(self.subject)
        await interaction.response.edit_message(
            embed=build_no_team_embed(can_create=False), view=TeamJoinView(self.owner_id, self.subject),
        )


class TeamJoinView(discord.ui.View):
    """No team yet: a Create button (managers only) plus a picker of every
    existing team to join."""

    def __init__(self, owner_id: int, subject: str, *, can_create: bool = False) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.subject = subject
        if can_create:
            create = discord.ui.Button(label="Create team", style=discord.ButtonStyle.primary)
            create.callback = self._create
            self.add_item(create)
        join = discord.ui.Button(label="Join a team", style=discord.ButtonStyle.secondary)
        join.callback = self._join
        self.add_item(join)
        standings = discord.ui.Button(label="Standings", style=discord.ButtonStyle.secondary)
        standings.callback = self._standings
        self.add_item(standings)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def _standings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=build_leaderboard_embed(), view=StandingsView(interaction.user.id),
            ephemeral=True)

    async def _create(self, interaction: discord.Interaction) -> None:
        async def submitted(inter: discord.Interaction, name: str) -> None:
            try:
                teams_core.create(self.subject, name)
            except teams_core.TeamError as err:
                await inter.response.send_message(f"Couldn't create a team: {err}", ephemeral=True)
                return
            team = teams_core.team_of(self.subject)
            await inter.response.edit_message(
                embed=build_team_embed(team), view=TeamManagerView(self.owner_id, self.subject),
            )

        await interaction.response.send_modal(_TeamNameModal("Create team", submitted))

    async def _join(self, interaction: discord.Interaction) -> None:
        all_teams = teams_core.list_teams()
        options = [(f"{t['name']} ({t['member_count']})"[:100], str(t["id"])) for t in all_teams]

        async def picked(inter: discord.Interaction, team_id_str: str) -> None:
            if team_id_str == "_none":
                await inter.response.send_message("There are no teams to join yet.", ephemeral=True)
                return
            try:
                teams_core.join(self.subject, int(team_id_str))
            except teams_core.TeamError as err:
                await inter.response.send_message(f"Couldn't join: {err}", ephemeral=True)
                return
            team = teams_core.team_of(self.subject)
            await inter.response.edit_message(
                embed=build_team_embed(team), view=TeamMemberView(self.owner_id, self.subject),
            )

        await interaction.response.send_message(
            "Which team?", view=OptionPickerView(self.owner_id, options, picked), ephemeral=True,
        )
