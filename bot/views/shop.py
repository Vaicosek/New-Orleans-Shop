"""Shop panel: stock table, item picker, price lookup, "order this".

"Order this" opens a production/restock order (`core.orders.create_order`) --
a customer or the owner asking for more of an item to be made, which a
worker later claims and delivers and a manager pays out on approval. No
money moves at order-creation time; that only happens at `/orders` approval.
"""
from __future__ import annotations

import discord

from core import catalog, orders as orders_core
from core.pricing import price_label

from . import orders as order_views
from .pickers import ItemPickerView
from ..ui.embed import money_text, panel_embed, price_line, rows


def build_shop_embed() -> discord.Embed:
    # Category -> subcategory -> item, same grouping the website's price
    # sheet uses -- `catalog.categories_with_items` is the one place that
    # order lives, so this never re-derives it and drifts from the site.
    cats = catalog.categories_with_items(active_only=True, include_empty=False)
    lines: list[str] = []
    any_stock = False
    for cat in cats:
        lines.append(f"**{cat['name']}**")
        for group in cat["groups"]:
            if group["subcategory"]:
                lines.append(f"_{group['subcategory']}_")
            for it in group["items"]:
                stock = catalog.get_stock(it["id"])
                qty = stock["pieces"]
                line = price_line(it["name"], it["price_coins"], it["price_unit_pieces"],
                                   it["stack_size"])
                # Omit the stock clause entirely for an unstocked item --
                # "(0/3456 in stock)" on every row when nothing is stocked
                # is noise, not information, and it wraps the row on a
                # phone screen for no reason.
                if qty > 0:
                    any_stock = True
                    line += f"  ({qty} in stock)"
                lines.append(line)

    if cats and not any_stock:
        body = "The shop isn't stocked yet."
    else:
        body = rows(lines, empty_text="No items in the catalog yet.")
    return panel_embed("New Orleans shop", body)


class _QuantityModal(discord.ui.Modal):
    """Pieces requested is genuinely free text (a quantity), never an
    identity -- the item itself is already resolved by the picker above."""

    def __init__(self, item: dict, requester: str, channel_id: int | None):
        super().__init__(title=f"Order: {item['name']}"[:45], timeout=300)
        self.item = item
        self.requester = requester
        self.channel_id = channel_id
        self.pieces = discord.ui.TextInput(
            label="How many pieces?", placeholder="e.g. 64", max_length=8, required=True
        )
        self.add_item(self.pieces)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pieces = int(str(self.pieces.value).strip())
            if pieces <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("Pieces must be a positive whole number.", ephemeral=True)
            return

        quote = catalog.quote(self.item["id"], pieces)
        try:
            order_id = orders_core.create_order(
                self.item["id"], pieces, created_by=self.requester,
                channel_id=str(self.channel_id) if self.channel_id else None,
            )
        except orders_core.OrderError as err:
            await interaction.followup.send(f"Could not open that order: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Opened order #{order_id}: {pieces} × {self.item['name']} "
            f"({quote['price_label']}, worth {money_text(quote['total_coins'])} "
            f"at payout). "
            f"Posted in the orders channel for workers to claim.",
            ephemeral=True,
        )

        channel = None
        if self.channel_id is not None:
            channel = interaction.client.get_channel(self.channel_id)
        if channel is not None:
            embed = order_views.build_order_embed(order_id)
            posted = await channel.send(embed=embed, view=order_views.OrderCardView())
            # Write the message back onto the order. Persistent buttons
            # re-resolve their subject from the message they sit on, so
            # without this the card can never be refreshed after approval.
            if posted is not None:
                orders_core.set_message(order_id, str(channel.id), str(posted.id))


class ShopPanelView(discord.ui.View):
    def __init__(self, owner_id: int, orders_channel_id: int | None) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.orders_channel_id = orders_channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Order this", style=discord.ButtonStyle.primary)
    async def order_this(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        from core import money as money_core

        async def picked(inter: discord.Interaction, item: dict) -> None:
            requester = money_core.user(inter.user.id)
            await inter.response.send_modal(_QuantityModal(item, requester, self.orders_channel_id))

        await interaction.response.send_message(
            "Search and pick an item to order:",
            view=ItemPickerView(self.owner_id, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Price lookup", style=discord.ButtonStyle.secondary)
    async def price_lookup(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, item: dict) -> None:
            await inter.response.send_message(
                f"{item['name']}: {price_label(item['price_coins'], item['price_unit_pieces'], item['stack_size'])}",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Search and pick an item to price:",
            view=ItemPickerView(self.owner_id, picked),
            ephemeral=True,
        )
