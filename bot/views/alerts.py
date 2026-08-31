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

from core import alerts, catalog, money

from .. import addressing
from ..permissions import is_staff
from ..ui.embed import panel_embed

_ADDRESS_MARK = re.compile(r"address (\S+)")


def _alert_footer(item_id: int, code: str) -> str:
    # The code alone -- never the raw item id next to it (see
    # bot/views/orders.py's `_order_footer` for the same fix and why).
    return f"address {code}"


def parse_item_id(message: discord.Message | None) -> int | None:
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer
    text = getattr(footer, "text", None)
    if not text:
        return None
    m = _ADDRESS_MARK.search(text)
    if not m:
        return None
    found = addressing.resolve(m.group(1))
    if found is None or found[0] != "item":
        return None
    try:
        return int(found[1])
    except (TypeError, ValueError):
        return None


def build_alert_embed(due_row: dict) -> discord.Embed:
    code = addressing.mint("item", due_row["item_id"])
    body = (
        f"{due_row['qty']} left, threshold is {due_row['threshold']} "
        f"(capacity {due_row['capacity']})."
    )
    return panel_embed(f"Restock: {due_row['name']}", body, tone="warn",
                        footer=_alert_footer(due_row["item_id"], code))


class AlertAckView(discord.ui.View):
    """Persistent. Registered once at boot with no item id attached.

    This card is PUBLIC, and acknowledging writes real suppression state --
    at 0 stock it silences the loudest alarm the shop has. So the button is
    staff-gated on the interacting user, re-checked on every click, exactly
    like `OrderCardView.approve_btn`: being able to see a message and press
    a button on it implies nothing about being allowed to."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Re-checked per click, and the config comes from the CLIENT: this
        # view is registered at boot with placeholder state, so `self` holds
        # no config to trust.
        config = getattr(interaction.client, "nola_config", None)
        if config is None or not is_staff(interaction.user, config):
            await interaction.response.send_message(
                "Acknowledging a restock alert is staff-only.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Acknowledge", style=discord.ButtonStyle.secondary,
                        custom_id="nola:alert:ack")
    async def ack_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        item_id = parse_item_id(interaction.message)
        if item_id is None:
            await interaction.response.send_message("Could not identify this item.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            alerts.acknowledge(item_id, actor=money.user(interaction.user.id))
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
                    embed=panel_embed(f"Restock: {item['name']}", body, tone="warn",
                                       footer=interaction.message.embeds[0].footer.text),
                )
        except discord.HTTPException:
            pass
