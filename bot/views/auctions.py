"""Auction board: the persistent public card posted in the auctions channel.

Same shape as bot/views/orders.py's order card: registered ONCE at boot with
no auction baked in (`bot.add_view(AuctionCardView())`), so every card
survives a Wispbyte restart, and every callback re-resolves the auction id
from `interaction.message`'s embed footer rather than from `self`.

There is deliberately no `/auction` panel and no separate "my bids" screen.
The card is the whole surface: anyone who can see it can bid on it with the
one button, and it already shows the current lead, so a second screen would
only repeat what is already on the message. Creating a lot (staff-only) and
voiding one both live in `/admin` instead, next to every other staff-only,
money-deciding action.
"""
from __future__ import annotations

import re
import secrets

import discord

from core import auctions as auctions_core
from core import money

from .. import addressing, queries
from ..ui.embed import SEP, money_text, panel_embed, rows

_ADDRESS_MARK = re.compile(r"address (\S+)")


def bidder_mention(subject: str) -> str:
    """Render a wallet subject ("u:<discord id>") as a `<@id>` mention.
    Same rule as bot/views/orders.py's worker_mention: the raw "u:<id>"
    form stays the subject used for every money-layer call, only what is
    PRINTED changes."""
    if isinstance(subject, str) and subject.startswith("u:"):
        discord_id = subject.split(":", 1)[1]
        if discord_id.isdigit():
            return f"<@{discord_id}>"
    return subject


def _auction_footer(auction_id: int, code: str) -> str:
    return f"address {code}"


def parse_auction_id(message: discord.Message | None) -> int | None:
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
    if found is None or found[0] != "auction":
        return None
    try:
        return int(found[1])
    except (TypeError, ValueError):
        return None


_STATUS_LABELS = {
    "open": "Open",
    "closed": "Closing...",
    "settled": "Settled",
    "voided": "Voided",
}


def build_auction_embed(auction_id: int) -> discord.Embed:
    auction = queries.get_auction_detail(auction_id)
    if auction is None:
        return panel_embed("Auction not found", "This listing no longer exists.", tone="loss")

    lines = [
        f"{auction['pieces']} piece(s)",
        f"Minimum bid {money_text(auction['min_bid'])}, "
        f"minimum raise {money_text(auction['min_increment'])}",
    ]
    if auction["leader_subject"] is not None:
        lines.append(
            f"Current lead: {bidder_mention(auction['leader_subject'])} "
            f"{SEP} {money_text(auction['leader_amount'])}"
        )
    else:
        lines.append("No bids yet.")
    lines.append(f"{auction['bid_count']} bid(s) total")

    if auction["status"] == "settled":
        if auction["winner"] is not None:
            lines.append(
                f"**Won by {bidder_mention(auction['winner'])} at "
                f"{money_text(auction['winning_amount'])}.**"
            )
        else:
            lines.append("**Closed with no bids.**")
    elif auction["status"] == "voided":
        lines.append("**Voided by staff. Every bid was refunded.**")
    else:
        lines.append(f"Closes: <t:{_epoch(auction['closes_at'])}:R>")

    code = addressing.mint("auction", auction_id)
    tone = "gain" if auction["status"] == "settled" and auction["winner"] else "neutral"
    return panel_embed(
        f"Auction: {auction['item_name']} ({_STATUS_LABELS.get(auction['status'], auction['status'])})",
        rows(lines),
        footer=_auction_footer(auction_id, code),
        tone=tone,
    )


def _epoch(sql_ts: str) -> int:
    """`closes_at` is stored as UTC 'YYYY-MM-DD HH:MM:SS'; Discord's `<t:...>`
    markup wants a Unix timestamp so every viewer sees it in their own
    timezone, converted client-side rather than baked in as UTC text."""
    from datetime import datetime, timezone
    dt = datetime.strptime(sql_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def auction_card_view(auction: dict) -> "AuctionCardView":
    """A card view with the Bid button disabled once the lot is no longer
    open -- reconstructed from a fresh read every refresh, same reasoning
    as order_card_view: a persistent view's buttons carry no per-auction
    state of their own."""
    view = AuctionCardView()
    if auction["status"] != "open":
        for child in view.children:
            child.disabled = True
    return view


class _BidAmountModal(discord.ui.Modal):
    """The bid amount is free text (a quantity); the auction itself is
    already resolved from the card's message before this opens."""

    def __init__(self, auction_id: int, item_name: str, subject: str) -> None:
        super().__init__(title=f"Bid: {item_name}"[:45], timeout=300)
        self.auction_id, self.subject = auction_id, subject
        self.amount = discord.ui.TextInput(
            label="Bid amount (g)", placeholder="e.g. 500", max_length=10, required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            amount = int(str(self.amount.value).strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("Amount must be a positive whole number.", ephemeral=True)
            return

        bal = money.balance(self.subject)
        if amount > bal.available:
            await interaction.followup.send(
                f"You only have {money_text(bal.available)} available.", ephemeral=True)
            return

        auction = queries.get_auction_detail(self.auction_id)
        if auction is None or auction["status"] != "open":
            await interaction.followup.send("This auction is no longer open.", ephemeral=True)
            return
        floor = (auction["min_bid"] if auction["leader_subject"] is None
                 else auction["leader_amount"] + auction["min_increment"])
        if amount < floor:
            await interaction.followup.send(
                f"Bid too low -- needs to be at least {money_text(floor)}.", ephemeral=True)
            return

        preview_id = secrets.token_hex(8)
        idem_key = f"auction.bid:{self.auction_id}:{amount}:{preview_id}"
        await interaction.followup.send(
            f"Bid {money_text(amount)} on \"{auction['item_name']}\"? This places a hold "
            "until you're outbid or the auction settles.",
            view=_BidConfirmGate(self.auction_id, auction["item_name"], self.subject, amount,
                                  idem_key=idem_key),
            ephemeral=True,
        )


class _BidConfirmGate(discord.ui.View):
    """Confirm-or-walk-away for one previewed bid. Same double-click guard
    as predict.py's `_StakeConfirmGate`: the button disables itself on the
    first click, and the actual placement runs under `money.guarded` on the
    preview-minted idempotency key, so two clicks resolve to the same bid,
    never two holds."""

    def __init__(self, auction_id: int, item_name: str, subject: str, amount: int, *,
                 idem_key: str) -> None:
        super().__init__(timeout=120)
        self.auction_id, self.item_name, self.subject = auction_id, item_name, subject
        self.amount = amount
        self.idem_key = idem_key
        self._submitted = False

    @discord.ui.button(label="Confirm bid", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if self._submitted:
            await interaction.response.send_message("That bid was already submitted.", ephemeral=True)
            return
        self._submitted = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            with money.guarded(self.idem_key, service="shop", endpoint="auctions.bid",
                                payload={"auction": self.auction_id, "amount": self.amount}) as g:
                if g.replay:
                    bid_id = g.response["bid_id"] if g.response else None
                else:
                    bid_id = auctions_core.bid(self.auction_id, self.subject, self.amount, conn=g.conn)
                    g.set_response({"bid_id": bid_id})
        except auctions_core.AuctionError as err:
            self._submitted = False
            button.disabled = False
            await interaction.followup.send(f"Could not place that bid: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not place that bid: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Bid {money_text(self.amount)} on \"{self.item_name}\" placed.", ephemeral=True)

        # Refresh the public card so every viewer sees the new lead without
        # needing to reopen it -- same pattern as orders' card refresh.
        auction = queries.get_auction_detail(self.auction_id)
        if auction and auction.get("channel_id") and auction.get("message_id"):
            try:
                channel = interaction.client.get_channel(int(auction["channel_id"]))
                if channel is None:
                    channel = await interaction.client.fetch_channel(int(auction["channel_id"]))
                message = await channel.fetch_message(int(auction["message_id"]))
                await message.edit(embed=build_auction_embed(self.auction_id),
                                    view=auction_card_view(auction))
            except discord.HTTPException:
                pass  # the bid itself already succeeded; a stale card is cosmetic


class AuctionCardView(discord.ui.View):
    """Persistent. Registered once at boot with no auction id attached --
    every callback re-resolves the auction from `interaction.message`."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Bid", style=discord.ButtonStyle.primary,
                        custom_id="nola:auction:bid")
    async def bid_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        auction_id = parse_auction_id(interaction.message)
        if auction_id is None:
            await interaction.response.send_message("Could not identify this auction.", ephemeral=True)
            return
        auction = queries.get_auction_detail(auction_id)
        if auction is None or auction["status"] != "open":
            await interaction.response.send_message("This auction is no longer open.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _BidAmountModal(auction_id, auction["item_name"], money.user(interaction.user.id))
        )
