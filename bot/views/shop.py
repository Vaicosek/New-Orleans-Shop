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
                # Every item is real content and gets a row -- CONTRACT.md
                # section 7 bars decorated absence, not real rows. Zero
                # stock is a fact about that one item, not a reason to drop
                # it (or the rest of the sheet) from the page.
                if qty > 0:
                    line += f"  ({qty} in stock)"
                else:
                    line += "  (out of stock)"
                lines.append(line)

    # The one-line message is for a genuinely empty catalog -- zero active
    # items, so `cats` itself came back empty -- never for a catalog that
    # has items but no stock on any of them; that case still renders the
    # full sheet above, every row marked "(out of stock)".
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

        # Post the card BEFORE telling the requester it's done -- the
        # success message used to be sent first and unconditionally, so a
        # missing channel, a permissions error, or a network hiccup on
        # `channel.send` failed silently after the requester had already
        # been told the order was posted for workers to claim. The order
        # itself (`order_id`) still exists and is still claimable through
        # `/orders` even if the card never posts -- only the PUBLIC card is
        # at risk here, not the order.
        channel = None
        if self.channel_id is not None:
            channel = interaction.client.get_channel(self.channel_id)

        post_note = ""
        if channel is None:
            post_note = (
                " Could not find the orders channel to post a public card -- "
                "the order still exists and can be worked from /orders."
            )
        else:
            try:
                embed = order_views.build_order_embed(order_id)
                posted = await channel.send(embed=embed, view=order_views.OrderCardView())
            except discord.HTTPException as err:
                post_note = (
                    f" Could not post the public order card ({err}) -- the order "
                    "still exists and can be worked from /orders."
                )
            else:
                # Write the message back onto the order. Persistent buttons
                # re-resolve their subject from the message they sit on, so
                # without this the card can never be refreshed after approval.
                if posted is not None:
                    orders_core.set_message(order_id, str(channel.id), str(posted.id))
                    post_note = " Posted in the orders channel for workers to claim."

        await interaction.followup.send(
            f"Opened order #{order_id}: {pieces} × {self.item['name']} "
            f"({quote['price_label']}, worth {money_text(quote['total_coins'])} "
            f"at payout).{post_note}",
            ephemeral=True,
        )


# The site's own address. Not a config value: it is the one place this shop
# lives and it is already in CONTRACT.md section 11 and on the domain itself,
# so an env var would be a setting nobody would ever change and one more thing
# that can be blank at boot.
SITE_URL = "https://neworleansshop.org/"


class ShopPanelView(discord.ui.View):
    def __init__(self, owner_id: int, orders_channel_id: int | None) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.orders_channel_id = orders_channel_id
        # The full sheet is a scrolling page with every category on it, and
        # Discord is a bad place to read one. A link button costs no round
        # trip and no callback -- Discord opens it itself -- and it is the
        # only affordance here that answers "just show me everything".
        self.add_item(discord.ui.Button(label="Full price sheet",
                                        style=discord.ButtonStyle.link,
                                        url=SITE_URL))

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
