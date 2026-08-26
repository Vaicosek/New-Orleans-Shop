"""Order board: the persistent order card posted in the orders channel, and
the ephemeral panel opened by `/orders`.

The card's Claim/Deliver/Approve buttons are registered ONCE at boot with no
order baked in (`bot.add_view(OrderCardView())`), so after a Wispbyte
restart every existing card message is still clickable. Every callback below
re-resolves the order id from `interaction.message`'s embed footer -- never
from `self` -- because `self` on a persistent view is whatever placeholder
state boot registration gave it, not the order that message is actually
about.

Approve pays real money out of `treasury:shop` and is reachable from a
PUBLIC message any member can see, so it is staff-gated on the interacting
user before anything else runs -- never assumed from context. The old
"type the item name back" step was never a real secret: `build_order_embed`
prints that same name in the card body two lines above the button, so
anyone who could see the card could pass the "confirmation". The actual
guard is the staff check plus a plain, numbers-first preview; approving is
otherwise a single confirm click, same shape as every other danger button
here.
"""
from __future__ import annotations

import re

import discord

from core import money, orders as orders_core
from core.pricing import price_label

from .. import addressing, queries
from ..permissions import is_staff
from ..ui.embed import SEP, money_text, panel_embed, rows

_ADDRESS_MARK = re.compile(r"address (\S+)")


def _order_footer(order_id: int, code: str) -> str:
    # The code alone -- never the raw order id next to it. `parse_order_id`
    # recovers the id by resolving this same code, the one place a card is
    # allowed to carry identity a user can also see.
    return f"address {code}"


def parse_order_id(message: discord.Message | None) -> int | None:
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
    if found is None or found[0] != "order":
        return None
    try:
        return int(found[1])
    except (TypeError, ValueError):
        return None


def build_order_embed(order_id: int) -> discord.Embed:
    order = queries.get_order_detail(order_id)
    if order is None:
        return panel_embed("Order not found", "This order no longer exists.")
    claims = orders_core.list_claims(order_id)
    label = price_label(order["price_coins"], order["price_unit_pieces"], order["stack_size"])
    claim_lines = [
        f"{c['worker']} {SEP} claimed {c['pieces']}, delivered {c['delivered']}"
        + (f" (paid {money_text(c['paid_coins'])})" if c["paid_coins"] else "")
        for c in claims
    ]
    body = (
        f"{order['item_name']}\n"
        f"{label}\n"
        f"Requested {order['requested_pieces']}  {SEP} produced {order['produced_pieces']}\n"
        f"Status: {order['status']}\n\n"
        f"{rows(claim_lines, empty_text='No claims yet.')}"
    )
    code = addressing.mint("order", order_id)
    e = panel_embed(f"Order #{order_id}", body, footer=_order_footer(order_id, code))
    return e


def order_card_view(order: dict) -> "OrderCardView":
    """A card view with buttons disabled to match `order`'s ACTUAL state.

    Every refresh reconstructs this from a fresh read rather than reusing
    whatever view instance was on the message before -- a persistent view's
    buttons carry no per-order state of their own, so "is this order still
    approvable" has to come from the row, not from the widget.
    """
    view = OrderCardView()
    closed = order["status"] in ("fulfilled", "cancelled")
    for child in view.children:
        custom_id = getattr(child, "custom_id", "")
        if closed:
            child.disabled = True
        elif custom_id == "nola:order:approve" and order["status"] != "awaiting_verification":
            child.disabled = True
    return view


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


async def _refresh_card(interaction: discord.Interaction, order_id: int) -> None:
    """Best-effort: update the channel card in place so it never shows stale
    status after an action taken from the panel rather than the card.

    Only correct for a SINGLE hop from the real card -- `interaction.message`
    here is whatever message the action was actually taken on, which is the
    real card for a modal opened directly off its button, but NOT for an
    action reached through an intermediate ephemeral message (see
    `_refresh_card_by_ref` for that case)."""
    try:
        order = queries.get_order_detail(order_id)
        if order is None:
            return
        if interaction.message is not None and parse_order_id(interaction.message) == order_id:
            embed = build_order_embed(order_id)
            await interaction.message.edit(embed=embed, view=order_card_view(order))
    except discord.HTTPException:
        pass


async def _refresh_card_by_ref(interaction: discord.Interaction, order_id: int,
                                channel_id: int | None, message_id: int | None) -> None:
    """Best-effort refresh keyed on the card's OWN channel/message id, never
    on `interaction.message`.

    Approve is reached from the card through an ephemeral confirm gate, so
    by the time this runs `interaction.message` is that ephemeral gate
    message, not the public card -- refreshing "whatever message this
    interaction is on" would silently do nothing. `channel_id`/`message_id`
    are captured back at the card's own button click, before that hop, and
    carried through the gate for exactly this."""
    if channel_id is None or message_id is None:
        return
    try:
        order = queries.get_order_detail(order_id)
        if order is None:
            return
        channel = interaction.client.get_channel(channel_id)
        if channel is None:
            channel = await interaction.client.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        await message.edit(embed=build_order_embed(order_id), view=order_card_view(order))
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

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.danger,
                        custom_id="nola:order:approve")
    async def approve_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        order_id = parse_order_id(interaction.message)
        if order_id is None:
            await interaction.response.send_message("Could not identify this order.", ephemeral=True)
            return
        # Staff gate FIRST, before anything else runs -- this button is on a
        # message any member of the server can see and click; nothing about
        # being able to press it implies being allowed to.
        config = getattr(interaction.client, "nola_config", None)
        if config is None or not is_staff(interaction.user, config):
            await interaction.response.send_message("Approving orders is staff-only.", ephemeral=True)
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
            view=_ApproveGate(order_id, money.user(interaction.user.id),
                               origin_channel_id=interaction.channel_id,
                               origin_message_id=interaction.message.id),
            ephemeral=True,
        )


class _ApproveGate(discord.ui.View):
    """No typed confirmation here -- the card printed the item name and
    price already, and the staff gate on the button that opened this is the
    real access control. A second danger-coloured click on real numbers is
    the confirmation; a name you can already read is not a secret."""

    def __init__(self, order_id: int, approver: str, *,
                 origin_channel_id: int | None = None, origin_message_id: int | None = None):
        super().__init__(timeout=120)
        self.order_id, self.approver = order_id, approver
        self.origin_channel_id = origin_channel_id
        self.origin_message_id = origin_message_id

    @discord.ui.button(label="Confirm approval", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            result = orders_core.approve(self.order_id, self.approver)
        except (orders_core.OrderError, money.MoneyError) as err:
            await interaction.followup.send(f"Could not approve: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Order #{self.order_id} approved {SEP} paid {money_text(result['paid_coins'])} "
            f"across {result['paid_claims']} claim(s).",
            ephemeral=True,
        )
        await _refresh_card_by_ref(interaction, self.order_id,
                                    self.origin_channel_id, self.origin_message_id)


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

    @discord.ui.button(label="Approve queue", style=discord.ButtonStyle.danger)
    async def approve_queue(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not is_staff(interaction.user, self.config):
            await interaction.response.send_message(
                "Approving orders is staff-only.", ephemeral=True
            )
            return
        pending = queries.list_orders(("awaiting_verification",))
        options = [(f"#{o['id']} {o['item_name']} {SEP} {o['produced_pieces']} pieces", str(o["id"]))
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
                view=_ApproveGate(order["id"], money.user(inter.user.id)),
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
        f"#{o['id']}  {o['item_name']}  {SEP} {o['produced_pieces']}/{o['requested_pieces']}  "
        f"({o['status']})"
        for o in open_orders
    ]
    return panel_embed("Orders board", rows(lines, empty_text="No open orders."))
