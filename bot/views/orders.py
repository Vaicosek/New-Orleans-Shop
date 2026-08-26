"""Order board: the persistent order card posted in the orders channel, and
the ephemeral panel opened by `/orders`.

The card's Claim/Deliver/Approve buttons are registered ONCE at boot with no
order baked in (`bot.add_view(OrderCardView())`), so after a Wispbyte
restart every existing card message is still clickable. Every callback below
re-resolves the order id from `interaction.message`'s embed footer -- never
from `self` -- because `self` on a persistent view is whatever placeholder
state boot registration gave it, not the order that message is actually
about.
"""
from __future__ import annotations

import re

import discord

from core import money, orders as orders_core
from core.pricing import price_label

from .. import addressing, queries
from ..permissions import is_staff
from ..ui.embed import panel_embed, rows

_ORDER_MARK = re.compile(r"order:(\d+)")


def _order_footer(order_id: int, code: str) -> str:
    return f"address {code}  ·  order:{order_id}"


def parse_order_id(message: discord.Message | None) -> int | None:
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer
    text = getattr(footer, "text", None)
    if not text:
        return None
    m = _ORDER_MARK.search(text)
    return int(m.group(1)) if m else None


def build_order_embed(order_id: int) -> discord.Embed:
    order = queries.get_order_detail(order_id)
    if order is None:
        return panel_embed("Order not found", "This order no longer exists.")
    claims = orders_core.list_claims(order_id)
    label = price_label(order["price_coins"], order["price_unit_pieces"], order["stack_size"])
    claim_lines = [
        f"{c['worker']} -- claimed {c['pieces']}, delivered {c['delivered']}"
        + (f" (paid {c['paid_coins']:,})" if c["paid_coins"] else "")
        for c in claims
    ]
    body = (
        f"{order['item_name']}\n"
        f"{label}\n"
        f"Requested {order['requested_pieces']}  ·  produced {order['produced_pieces']}\n"
        f"Status: {order['status']}\n\n"
        f"{rows(claim_lines, empty_text='No claims yet.')}"
    )
    code = addressing.mint("order", order_id)
    e = panel_embed(f"Order #{order_id}", body, footer=_order_footer(order_id, code))
    return e


class _PiecesModal(discord.ui.Modal):
    """Free-text quantity -- allowed because a piece count is genuinely free
    text, not an identity. The order itself is already resolved by the
    caller before this modal ever opens."""

    def __init__(self, title: str, order_id: int, worker: str, action: str):
        super().__init__(title=title, timeout=300)
        self.order_id = order_id
        self.worker = worker
        self.action = action
        self.pieces = discord.ui.TextInput(
            label="Pieces", placeholder="e.g. 64", max_length=8, required=True
        )
        self.add_item(self.pieces)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pieces = int(str(self.pieces.value).strip())
        except ValueError:
            await interaction.followup.send("Pieces must be a whole number.", ephemeral=True)
            return
        try:
            if self.action == "claim":
                orders_core.claim(self.order_id, self.worker, pieces)
                msg = f"Claimed {pieces} pieces of order #{self.order_id}."
            else:
                new_status = orders_core.mark_fulfilled(self.order_id, self.worker, pieces)
                msg = f"Recorded {pieces} pieces delivered on order #{self.order_id} ({new_status})."
        except orders_core.OrderError as err:
            await interaction.followup.send(f"Could not do that: {err}", ephemeral=True)
            return
        await interaction.followup.send(msg, ephemeral=True)
        await _refresh_card(interaction, self.order_id)


class ApproveConfirmModal(discord.ui.Modal):
    """The irreversible step. The typed confirmation string is the item's
    NAME, never the order id -- CONTRACT.md section 7."""

    def __init__(self, order_id: int, approver: str, item_name: str):
        super().__init__(title=f"Confirm approval: order #{order_id}", timeout=300)
        self.order_id = order_id
        self.approver = approver
        self.item_name = item_name
        self.confirm = discord.ui.TextInput(
            label=f"Type the item name to confirm: {item_name}",
            placeholder=item_name, max_length=100, required=True,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.item_name.strip().lower():
            await interaction.followup.send(
                "That doesn't match the item name -- approval cancelled.", ephemeral=True
            )
            return
        try:
            result = orders_core.approve(self.order_id, self.approver)
        except orders_core.OrderError as err:
            await interaction.followup.send(f"Could not approve: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Order #{self.order_id} approved -- paid {result['paid_coins']:,} coins "
            f"across {result['paid_claims']} claim(s).",
            ephemeral=True,
        )
        await _refresh_card(interaction, self.order_id)


async def _refresh_card(interaction: discord.Interaction, order_id: int) -> None:
    """Best-effort: update the channel card in place so it never shows stale
    status after an action taken from the panel rather than the card."""
    try:
        embed = build_order_embed(order_id)
        if interaction.message is not None and parse_order_id(interaction.message) == order_id:
            await interaction.message.edit(embed=embed, view=OrderCardView())
    except discord.HTTPException:
        pass


class OrderCardView(discord.ui.View):
    """Persistent. Registered once at boot with no order id attached --
    every callback re-resolves the order from `interaction.message`."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary,
                        custom_id="nola:order:claim")
    async def claim_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        order_id = parse_order_id(interaction.message)
        if order_id is None:
            await interaction.response.send_message("Could not identify this order.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _PiecesModal("Claim pieces", order_id, money.user(interaction.user.id), "claim")
        )

    @discord.ui.button(label="Mark delivered", style=discord.ButtonStyle.secondary,
                        custom_id="nola:order:deliver")
    async def deliver_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        order_id = parse_order_id(interaction.message)
        if order_id is None:
            await interaction.response.send_message("Could not identify this order.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _PiecesModal("Pieces delivered", order_id, money.user(interaction.user.id), "deliver")
        )

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success,
                        custom_id="nola:order:approve")
    async def approve_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        order_id = parse_order_id(interaction.message)
        if order_id is None:
            await interaction.response.send_message("Could not identify this order.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        order = queries.get_order_detail(order_id)
        if order is None:
            await interaction.followup.send("This order no longer exists.", ephemeral=True)
            return
        if order["status"] != "awaiting_verification":
            await interaction.followup.send(
                f"Order #{order_id} is {order['status']}, not ready to approve.", ephemeral=True
            )
            return
        label = price_label(order["price_coins"], order["price_unit_pieces"], order["stack_size"])
        await interaction.followup.send(
            f"Approving order #{order_id} ({order['item_name']}, {label}) will pay every "
            f"claim's delivered pieces from the shop treasury. Confirm below.",
            view=_ApproveGate(order_id, money.user(interaction.user.id), order["item_name"]),
            ephemeral=True,
        )


class _ApproveGate(discord.ui.View):
    def __init__(self, order_id: int, approver: str, item_name: str) -> None:
        super().__init__(timeout=120)
        self.order_id, self.approver, self.item_name = order_id, approver, item_name

    @discord.ui.button(label="Confirm approval", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ApproveConfirmModal(self.order_id, self.approver, self.item_name)
        )


class OrdersPanelView(discord.ui.View):
    """`/orders` panel. Board is display-only; every action still routes
    through the same picker -> modal flow as the channel card."""

    def __init__(self, owner_id: int, config) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.config = config

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Claim an order", style=discord.ButtonStyle.primary)
    async def claim_order(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        open_orders = queries.list_orders(("open", "claimed"))
        options = [
            (f"#{o['id']} {o['item_name']} ({o['produced_pieces']}/{o['requested_pieces']})",
             str(o["id"]))
            for o in open_orders
        ]

        async def picked(inter: discord.Interaction, order_id_str: str) -> None:
            if order_id_str == "_none":
                await inter.response.send_message("No open orders.", ephemeral=True)
                return
            await inter.response.send_modal(
                _PiecesModal("Claim pieces", int(order_id_str),
                             money.user(inter.user.id), "claim")
            )

        from .pickers import OptionPickerView
        await interaction.response.send_message(
            "Pick an order to claim:",
            view=OptionPickerView(self.owner_id, options, picked, placeholder="Choose an order..."),
            ephemeral=True,
        )

    @discord.ui.button(label="Mark delivered", style=discord.ButtonStyle.secondary)
    async def deliver(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        worker = money.user(interaction.user.id)
        mine = queries.list_orders(("claimed", "awaiting_verification"), worker=worker)
        options = [
            (f"#{o['id']} {o['item_name']} ({o['produced_pieces']}/{o['requested_pieces']})",
             str(o["id"]))
            for o in mine
        ]

        async def picked(inter: discord.Interaction, order_id_str: str) -> None:
            if order_id_str == "_none":
                await inter.response.send_message("You have no claimed orders.", ephemeral=True)
                return
            await inter.response.send_modal(
                _PiecesModal("Pieces delivered", int(order_id_str), worker, "deliver")
            )

        from .pickers import OptionPickerView
        await interaction.response.send_message(
            "Pick a claimed order to report delivery on:",
            view=OptionPickerView(self.owner_id, options, picked, placeholder="Choose an order..."),
            ephemeral=True,
        )

    @discord.ui.button(label="Approve queue", style=discord.ButtonStyle.success)
    async def approve_queue(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not is_staff(interaction.user, self.config):
            await interaction.response.send_message(
                "Approving orders is staff-only.", ephemeral=True
            )
            return
        pending = queries.list_orders(("awaiting_verification",))
        options = [(f"#{o['id']} {o['item_name']} -- {o['produced_pieces']} pieces", str(o["id"]))
                   for o in pending]

        async def picked(inter: discord.Interaction, order_id_str: str) -> None:
            if order_id_str == "_none":
                await inter.response.send_message("Nothing awaiting approval.", ephemeral=True)
                return
            order = queries.get_order_detail(int(order_id_str))
            label = price_label(order["price_coins"], order["price_unit_pieces"], order["stack_size"])
            await inter.response.send_message(
                f"Approving order #{order['id']} ({order['item_name']}, {label}) will pay every "
                f"claim's delivered pieces from the shop treasury. Confirm below.",
                view=_ApproveGate(order["id"], money.user(inter.user.id), order["item_name"]),
                ephemeral=True,
            )

        from .pickers import OptionPickerView
        await interaction.response.send_message(
            "Pick an order to approve:",
            view=OptionPickerView(self.owner_id, options, picked, placeholder="Choose an order..."),
            ephemeral=True,
        )


def build_panel_embed() -> discord.Embed:
    open_orders = queries.list_orders(("open", "claimed", "awaiting_verification"), limit=15)
    lines = [
        f"#{o['id']}  {o['item_name']}  --  {o['produced_pieces']}/{o['requested_pieces']}  "
        f"({o['status']})"
        for o in open_orders
    ]
    return panel_embed("Orders board", rows(lines, empty_text="No open orders."))
