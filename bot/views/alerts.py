"""Restock alert card: persistent Acknowledge button.

AbexTech's Acknowledge button disables components on one message and writes
nothing, so the same DM repeats every scan. Here acknowledging calls
`core.alerts.acknowledge`, which writes `acked_until_qty` -- real state, not
a disabled button. The item id is re-resolved from the message footer, never
from `self`, so this still works after a restart.
"""
from __future__ import annotations

import re

import discord

from core import alerts, catalog

from .. import addressing
from ..ui.embed import panel_embed

_ITEM_MARK = re.compile(r"item:(\d+)")


def _alert_footer(item_id: int, code: str) -> str:
    return f"address {code}  ·  item:{item_id}"


def parse_item_id(message: discord.Message | None) -> int | None:
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer
    text = getattr(footer, "text", None)
    if not text:
        return None
    m = _ITEM_MARK.search(text)
    return int(m.group(1)) if m else None


def build_alert_embed(due_row: dict) -> discord.Embed:
    code = addressing.mint("item", due_row["item_id"])
    body = (
        f"{due_row['qty']} left, threshold is {due_row['threshold']} "
        f"(capacity {due_row['capacity']})."
    )
    return panel_embed(f"Restock: {due_row['name']}", body,
                        footer=_alert_footer(due_row["item_id"], code))


class AlertAckView(discord.ui.View):
    """Persistent. Registered once at boot with no item id attached."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Acknowledge", style=discord.ButtonStyle.secondary,
                        custom_id="nola:alert:ack")
    async def ack_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        item_id = parse_item_id(interaction.message)
        if item_id is None:
            await interaction.response.send_message("Could not identify this item.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            alerts.acknowledge(item_id)
        except alerts.AlertError as err:
            await interaction.followup.send(f"Could not acknowledge: {err}", ephemeral=True)
            return
        item = catalog.get_item(item_id)
        await interaction.followup.send(
            f"Acknowledged. {item['name']} will alert again only if stock drops further.",
            ephemeral=True,
        )
        try:
            if interaction.message is not None:
                stock = catalog.get_stock(item_id)
                body = f"Acknowledged at {stock['pieces']} left. Will re-fire if it gets worse."
                await interaction.message.edit(
                    embed=panel_embed(f"Restock: {item['name']}", body,
                                       footer=interaction.message.embeds[0].footer.text),
                )
        except discord.HTTPException:
            pass
