"""Shop panel: stock table, item picker, price lookup, "order this".

"Order this" opens a production/restock order (`core.orders.create_order`) --
a customer or the owner asking for more of an item to be made, which a
worker later claims and delivers and a manager pays out on approval. No
money moves at order-creation time; that only happens at `/orders` approval.
"""
from __future__ import annotations

import discord

from core import catalog, orders as orders_core, teams as teams_core
from core.pricing import charge, price_label

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


def _payout_total(order_id: int) -> int:
    """What this order will pay its workers in total, read off the order's
    own snapshot -- never recomputed from the item's live price, which is
    the SELL price and a different number entirely."""
    try:
        order = orders_core.get_order(order_id)
        return charge(order["requested_pieces"],
                      order["payout_coins"] or order["price_coins"],
                      order["price_unit_pieces"])
    except Exception:  # noqa: BLE001 -- a confirmation must not fail over a figure
        return 0


def _ping_for(category: object) -> str:
    """The teams to put in front of a new order card, by name.

    The whole point of a team declaring what it works: an order for ores
    reaches the ore crew instead of every member of the server reading
    every order. A team that has declared nothing works everything and is
    included; a team with no members is not, since mentioning an empty
    roster is noise for everybody else.

    Team NAMES, not member mentions. A roster of fourteen would put
    fourteen pings on one card, which is the "@everyone for every order"
    problem wearing a different hat -- the name tells the right people it
    is theirs, and the card is already in the channel they watch. Nothing
    is mentioned at all when the category matches every team, because a
    ping that always fires is one nobody reads.
    """
    if not category:
        return ""
    try:
        matched = teams_core.teams_for_category(str(category))
        total = sum(1 for row in teams_core.leaderboard() if row["member_count"])
    except Exception:  # noqa: BLE001 -- a card that posts beats a perfect mention
        return ""
    if not matched or (total and len(matched) >= total):
        return ""
    names = ", ".join(row["name"] for row in matched[:5])
    return f"For {names}"


class _QuantityModal(discord.ui.Modal):
    """Pieces requested is genuinely free text (a quantity), never an
    identity -- the item itself is already resolved by the picker above."""

    def __init__(self, item: dict, requester: str, channel_id: int | None):
        super().__init__(title=f"Order: {item['name']}"[:45], timeout=300)
        self.item = item
        self.requester = requester
        self.channel_id = channel_id
        stack = int(item.get("stack_size") or 1)
        self.stack_size = stack
        self.barrel_slots = int(item.get("barrel_slots") or 0)
        self.barrel_pieces = self.barrel_slots * stack
        # A modal may only hold text inputs -- no dropdown -- so the unit
        # rides in the field itself rather than costing a whole extra
        # picker step ahead of the modal. Goods here are quoted per stack
        # ("1 g / stack of 64"), and making somebody multiply that out to
        # order is where a mis-order comes from.
        if stack > 1:
            label = "How many? (pieces, stacks or barrels)"
            placeholder = f"e.g. 64, or 3 stacks, or 1 barrel ({self.barrel_pieces:,})"
        else:
            label, placeholder = "How many pieces?", "e.g. 64"
        self.pieces = discord.ui.TextInput(
            label=label[:45], placeholder=placeholder, max_length=16, required=True
        )
        self.add_item(self.pieces)
        self.wanted = discord.ui.TextInput(
            label="Wanted within how many days? (optional)",
            placeholder="blank = whenever; unclaimed past it is dropped",
            max_length=3, required=False,
        )
        self.add_item(self.wanted)

    def _parse_quantity(self, raw: str) -> tuple[int, str]:
        """Return (pieces, what_they_asked_for) from the typed quantity.

        Accepts a bare count in pieces, or a count followed by a stack word
        -- "3 stacks", "3 stack", "3s", "3 st". `stack_size` MULTIPLIES a
        count here; it never divides a price (that is `price_unit_pieces`
        alone, and the two were one column once, which is how saplings at
        1 g per 32 came out half-priced). Raises ValueError for anything
        else, including a stack unit on an item that does not stack, so the
        caller can say so plainly rather than silently ordering pieces.
        """
        text = " ".join(str(raw).strip().lower().split())
        unit = "pieces"
        # Longest suffixes first: "barrels" must be tested before "b", and
        # "stacks" before "s", or "3 barrels" parses as 3 somethings-else.
        for suffix, found in (("barrels", "barrels"), ("barrel", "barrels"),
                               ("stacks", "stacks"), ("stack", "stacks"),
                               ("st", "stacks"), ("b", "barrels"), ("s", "stacks")):
            if text.endswith(suffix):
                head = text[: -len(suffix)].strip()
                if head:
                    text, unit = head, found
                    break
        count = int(text)
        if count <= 0:
            raise ValueError("quantity must be positive")
        if unit == "pieces":
            return count, f"{count:,} pieces"
        if unit == "stacks":
            if self.stack_size <= 1:
                raise ValueError(f"{self.item['name']} does not come in stacks")
            pieces = count * self.stack_size
            return pieces, f"{count:,} stack{'s' if count != 1 else ''} ({pieces:,} pieces)"
        if self.barrel_pieces <= 1:
            raise ValueError(f"{self.item['name']} has no barrel size on record")
        pieces = count * self.barrel_pieces
        return pieces, f"{count:,} barrel{'s' if count != 1 else ''} ({pieces:,} pieces)"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pieces, asked_for = self._parse_quantity(self.pieces.value)
        except ValueError as err:
            hint = (" You can type a piece count, a stack count like \"3 stacks\", "
                    "or a barrel count like \"1 barrel\"."
                    if self.stack_size > 1 else "")
            detail = str(err) if str(err) and "invalid literal" not in str(err) else ""
            await interaction.followup.send(
                (f"Could not read that quantity{': ' + detail if detail else ''}."
                 f"{hint}"), ephemeral=True)
            return

        wanted_days = None
        wanted_raw = str(self.wanted.value or "").strip()
        if wanted_raw:
            try:
                wanted_days = int(wanted_raw)
            except ValueError:
                await interaction.followup.send(
                    "Days must be a whole number, or leave it blank.", ephemeral=True)
                return
            if not 1 <= wanted_days <= 90:
                await interaction.followup.send(
                    "Days must be between 1 and 90, or leave it blank.", ephemeral=True)
                return

        quote = catalog.quote(self.item["id"], pieces)
        try:
            order_id = orders_core.create_order(
                self.item["id"], pieces, created_by=self.requester,
                channel_id=str(self.channel_id) if self.channel_id else None,
                wanted_in_days=wanted_days,
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
                posted = await channel.send(
                    content=_ping_for(self.item.get("category")) or None,
                    embed=embed, view=order_views.OrderCardView(),
                )
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
            f"Opened order #{order_id}: {asked_for} of {self.item['name']} "
            f"(sells at {quote['price_label']}; pays workers "
            f"{money_text(_payout_total(order_id))}).{post_note}",
            ephemeral=True,
        )


# The site's own address. Not a config value: it is the one place this shop
# lives and it is already in CONTRACT.md section 12 and on the domain itself,
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
