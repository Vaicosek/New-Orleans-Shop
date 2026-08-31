"""`/setup` -- preview the layout, then build it.

Section 8 rule 10 applies even though no money moves: preview with real
figures, then confirm. Creating twelve channels and two roles in someone's
server is not reversible by a button, so the owner sees exactly what will be
created, what will be adopted, and what is already fine, BEFORE anything
happens.

Two Discord constraints shape this file:

- **Defer inside 3 seconds.** Building the layout is a dozen sequential API
  calls and will blow the interaction window several times over. The button
  defers first and reports progress by editing its own message.
- **The confirm button re-resolves its subject.** It re-plans from the live
  guild at press time rather than trusting the plan the preview was built
  from. Someone can delete a channel between preview and press, and a plan
  built thirty seconds ago would then create a duplicate or skip a hole.
"""
from __future__ import annotations

import discord

from core import provision
from core.config import BotConfig
from core.provision import Step

from ..ui.embed import panel_embed

ACTION_GLYPH = {"create": "+", "adopt": "~", "ok": "="}


def snapshot(guild: discord.Guild) -> tuple[list[int], dict[tuple[str, str], int]]:
    """What exists in the guild right now, in the shape `provision.plan` wants.

    Categories are `CategoryChannel`, which is also a channel, so they are
    matched on their own kind -- otherwise a category called "Staff" would be
    adopted as the #staff channel and the layout would nest wrongly.
    """
    live: list[int] = [c.id for c in guild.channels] + [r.id for r in guild.roles]
    by_name: dict[tuple[str, str], int] = {}
    for c in guild.channels:
        kind = "category" if isinstance(c, discord.CategoryChannel) else "channel"
        by_name.setdefault((kind, c.name.lower()), c.id)
    for r in guild.roles:
        if not r.is_default():
            by_name.setdefault(("role", r.name.lower()), r.id)
    return live, by_name


def plan_for(guild: discord.Guild) -> list[Step]:
    live, by_name = snapshot(guild)
    return provision.plan(guild.id, live_ids=live, existing_by_name=by_name)


def missing_permissions(guild: discord.Guild) -> list[str]:
    """Named up front rather than discovered halfway through a build that then
    leaves the server half-provisioned."""
    me = guild.me
    if me is None:
        return ["(the bot is not in this guild)"]
    perms = me.guild_permissions
    missing = []
    if not perms.manage_channels:
        missing.append("Manage Channels")
    if not perms.manage_roles:
        missing.append("Manage Roles")
    return missing


def build_setup_embed(steps: list[Step], guild: discord.Guild) -> discord.Embed:
    counts = provision.summarise(steps)
    embed = panel_embed(
        "Server setup",
        (
            f"**{counts['create']}** to create · **{counts['adopt']}** to adopt · "
            f"**{counts['ok']}** already in place"
        ),
        tone="brand",
    )
    lines = []
    for s in steps:
        glyph = ACTION_GLYPH[s.action]
        label = s.desired.name if s.desired.kind == "role" else f"#{s.desired.name}"
        if s.desired.kind == "category":
            label = s.desired.name.upper()
        note = ""
        if s.action == "adopt":
            note = "  (existing, will be used as-is)"
        elif s.action == "ok":
            note = "  (already built)"
        lines.append(f"`{glyph}` {label}{note}")
    embed.add_field(name="Layout", value="\n".join(lines)[:1024], inline=False)

    gaps = missing_permissions(guild)
    if gaps:
        embed.add_field(
            name="Cannot build yet",
            value="The bot is missing: " + ", ".join(gaps),
            inline=False,
        )
    elif counts["create"] == 0 and counts["adopt"] == 0:
        embed.add_field(
            name="Nothing to do",
            value="This server is already set up.",
            inline=False,
        )
    return embed


class SetupConfirmView(discord.ui.View):
    """Not persistent on purpose: this is a one-shot confirmation, and a
    button that survives a restart would apply a plan computed before it."""

    def __init__(self, owner_id: int, config: BotConfig) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.config = config

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This panel belongs to whoever opened it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Build it", style=discord.ButtonStyle.primary)
    async def build(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Defer FIRST: a dozen create calls will not finish inside 3 seconds.
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Run this inside the server.", ephemeral=True)
            return

        gaps = missing_permissions(guild)
        if gaps:
            await interaction.followup.send(
                "Cannot build -- the bot is missing: " + ", ".join(gaps), ephemeral=True
            )
            return

        # Re-plan against the guild as it is NOW, not as it was at preview.
        steps = plan_for(guild)
        created, adopted, failed = await apply_plan(guild, steps)

        lines = [f"Created **{created}**, adopted **{adopted}**."]
        if failed:
            lines.append("")
            lines.append("**Failed:**")
            lines.extend(f"`x` {key} — {err}" for key, err in failed)
            lines.append("")
            lines.append("Run `/setup` again — it resumes from where it stopped.")
        else:
            lines.append("The layout is complete. Nothing else needs an id in `.env`.")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Setup cancelled.", embed=None, view=None)
        self.stop()


async def apply_plan(
    guild: discord.Guild, steps: list[Step]
) -> tuple[int, int, list[tuple[str, str]]]:
    """Create what is missing, record what exists. Returns (created, adopted, failures).

    Each item is recorded IMMEDIATELY after it is created, not in a batch at
    the end. A failure halfway through then leaves a partial layout that the
    next `/setup` sees as 'ok' and continues from -- rather than a set of
    orphaned channels the bot has no record of and would build again.
    """
    created = adopted = 0
    failures: list[tuple[str, str]] = []
    categories: dict[str, discord.CategoryChannel] = {}
    roles: dict[str, discord.Role] = {}

    for step in steps:
        d = step.desired
        try:
            if step.action == "ok":
                obj = guild.get_channel(step.existing_id) or guild.get_role(step.existing_id)
                if isinstance(obj, discord.CategoryChannel):
                    categories[d.key] = obj
                elif isinstance(obj, discord.Role):
                    roles[d.key] = obj
                continue

            if step.action == "adopt":
                obj = guild.get_channel(step.existing_id) or guild.get_role(step.existing_id)
                if obj is None:
                    failures.append((d.key, "vanished between preview and build"))
                    continue
                provision.record(guild.id, d.key, obj.id, getattr(obj, "name", d.name))
                if isinstance(obj, discord.CategoryChannel):
                    categories[d.key] = obj
                elif isinstance(obj, discord.Role):
                    roles[d.key] = obj
                adopted += 1
                continue

            overwrites = _overwrites(guild, d.staff_only, roles)
            if d.kind == "role":
                made = await guild.create_role(name=d.name, hoist=True,
                                               reason="New Orleans /setup")
                roles[d.key] = made
            elif d.kind == "category":
                made = await guild.create_category(name=d.name, overwrites=overwrites,
                                                   reason="New Orleans /setup")
                categories[d.key] = made
            else:
                made = await guild.create_text_channel(
                    name=d.name,
                    category=categories.get(d.parent) if d.parent else None,
                    topic=d.topic,
                    overwrites=overwrites,
                    reason="New Orleans /setup",
                )
            provision.record(guild.id, d.key, made.id, made.name)
            created += 1
        except discord.Forbidden:
            failures.append((d.key, "the bot lacks permission"))
        except discord.HTTPException as err:
            failures.append((d.key, f"Discord refused it: {err.text or err}"))

    return created, adopted, failures


def _overwrites(
    guild: discord.Guild, staff_only: bool, roles: dict[str, discord.Role]
) -> dict:
    """Staff-only things are hidden from @everyone and shown to Staff and
    Manager. Roles are created before any staff-only channel in DESIRED order,
    so they are in `roles` by the time this is called for one."""
    if not staff_only:
        return {}
    ov = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for key in ("role:staff", "role:manager"):
        role = roles.get(key)
        if role is not None:
            ov[role] = discord.PermissionOverwrite(view_channel=True)
    return ov
