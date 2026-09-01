"""Land board: the persistent public card posted in the land channel.

Same shape as bot/views/auctions.py's AuctionCardView, mirrored rather than
reused: registered ONCE at boot with no listing baked in
(`bot.add_view(LandCardView())`), so every card survives a Wispbyte
restart, and every callback re-resolves the listing id from
`interaction.message`'s embed footer rather than from `self`.

No `/land` panel, same reasoning as auctions -- the card IS the surface.
Listing a plot (staff-only) and voiding one both live in `/admin`, next to
"Open auction"/"Void auction".
"""
from __future__ import annotations

import re
import secrets

import discord

from core import land as land_core
from core import money

from .. import addressing, queries
from ..ui.embed import SEP, money_text, panel_embed, rows

_ADDRESS_MARK = re.compile(r"address (\S+)")


def bidder_mention(subject: str) -> str:
    """Same rule as bot/views/auctions.py's `bidder_mention`."""
    if isinstance(subject, str) and subject.startswith("u:"):
        discord_id = subject.split(":", 1)[1]
        if discord_id.isdigit():
            return f"<@{discord_id}>"
    return subject


def _land_footer(land_id: int, code: str) -> str:
    return f"address {code}"


def parse_land_id(message: discord.Message | None) -> int | None:
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
    if found is None or found[0] != "land":
        return None
    try:
        return int(found[1])
    except (TypeError, ValueError):
        return None


_STATUS_LABELS = {
    "open": "Open",
    "closed": "Closing...",
    "settled": "Sold",
    "voided": "Voided",
}


def build_land_embed(land_id: int) -> discord.Embed:
    listing = queries.get_land_detail(land_id)
    if listing is None:
        return panel_embed("Listing not found", "This plot is no longer listed.", tone="loss")

    lines = []
    if listing["description"]:
        lines.append(listing["description"])
    if listing["location"]:
        lines.append(f"Location: {listing['location']}")
    lines.append(
        f"Minimum bid {money_text(listing['min_bid'])}, "
        f"minimum raise {money_text(listing['min_increment'])}"
    )
    if listing["buy_now_price"] is not None:
        lines.append(f"Buy now: {money_text(listing['buy_now_price'])}")
    if listing["leader_subject"] is not None:
        lines.append(
            f"Current lead: {bidder_mention(listing['leader_subject'])} "
            f"{SEP} {money_text(listing['leader_amount'])}"
        )
    else:
        lines.append("No bids yet.")
    lines.append(f"{listing['bid_count']} bid(s) total")

    if listing["status"] == "settled":
        if listing["winner"] is not None:
            lines.append(
                f"**Sold to {bidder_mention(listing['winner'])} at "
                f"{money_text(listing['winning_amount'])}.**"
            )
        else:
            lines.append("**Closed with no bids.**")
    elif listing["status"] == "voided":
        lines.append("**Voided by staff. Every bid was refunded.**")
    else:
        lines.append(f"Closes: <t:{_epoch(listing['closes_at'])}:R>")

    code = addressing.mint("land", land_id)
    tone = "gain" if listing["status"] == "settled" and listing["winner"] else "neutral"
    return panel_embed(
        f"Land: {listing['name']} ({_STATUS_LABELS.get(listing['status'], listing['status'])})",
        rows(lines),
        footer=_land_footer(land_id, code),
        tone=tone,
    )


def _epoch(sql_ts: str) -> int:
    """Same conversion as bot/views/auctions.py's `_epoch`."""
    from datetime import datetime, timezone
    dt = datetime.strptime(sql_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def land_card_view(listing: dict) -> "LandCardView":
    """Same reasoning as `auction_card_view`: reconstructed from a fresh
    read every refresh, buttons carry no per-listing state of their own."""
    view = LandCardView()
    if listing["status"] != "open":
        for child in view.children:
            child.disabled = True
    return view


class _BidAmountModal(discord.ui.Modal):
    """The bid amount is free text; the listing itself is already resolved
    from the card's message before this opens."""

    def __init__(self, land_id: int, land_name: str, subject: str) -> None:
        super().__init__(title=f"Bid: {land_name}"[:45], timeout=300)
        self.land_id, self.subject = land_id, subject
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

        listing = queries.get_land_detail(self.land_id)
        if listing is None or listing["status"] != "open":
            await interaction.followup.send("This listing is no longer open.", ephemeral=True)
            return
        floor = (listing["min_bid"] if listing["leader_subject"] is None
                 else listing["leader_amount"] + listing["min_increment"])
        if amount < floor:
            await interaction.followup.send(
                f"Bid too low -- needs to be at least {money_text(floor)}.", ephemeral=True)
            return

        preview_id = secrets.token_hex(8)
        idem_key = f"land.bid:{self.land_id}:{amount}:{preview_id}"
        note = ""
        if listing["buy_now_price"] is not None and amount >= listing["buy_now_price"]:
            note = " This meets the buy-now price -- it will sell instantly."
        await interaction.followup.send(
            f"Bid {money_text(amount)} on \"{listing['name']}\"? This places a hold "
            f"until you're outbid or the listing settles.{note}",
            view=_BidConfirmGate(self.land_id, listing["name"], self.subject, amount,
                                  idem_key=idem_key),
            ephemeral=True,
        )


class _BidConfirmGate(discord.ui.View):
    """Confirm-or-walk-away for one previewed bid. Same double-click guard
    as bot/views/auctions.py's `_BidConfirmGate`."""

    def __init__(self, land_id: int, land_name: str, subject: str, amount: int, *,
                 idem_key: str) -> None:
        super().__init__(timeout=120)
        self.land_id, self.land_name, self.subject = land_id, land_name, subject
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
            with money.guarded(self.idem_key, service="shop", endpoint="land.bid",
                                payload={"land": self.land_id, "amount": self.amount}) as g:
                if g.replay:
                    result = g.response
                else:
                    result = land_core.bid(self.land_id, self.subject, self.amount, conn=g.conn)
                    g.set_response(result)
        except land_core.LandError as err:
            self._submitted = False
            button.disabled = False
            await interaction.followup.send(f"Could not place that bid: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not place that bid: {err}", ephemeral=True)
            return

        if result and result.get("bought_now"):
            await interaction.followup.send(
                f"Bid {money_text(self.amount)} on \"{self.land_name}\" met the buy-now price -- "
                "the plot is yours.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Bid {money_text(self.amount)} on \"{self.land_name}\" placed.", ephemeral=True)

        # Refresh the public card so every viewer sees the new lead (or the
        # sale) without needing to reopen it -- same pattern as auctions'
        # bid confirm gate.
        listing = queries.get_land_detail(self.land_id)
        if listing and listing.get("channel_id") and listing.get("message_id"):
            try:
                channel = interaction.client.get_channel(int(listing["channel_id"]))
                if channel is None:
                    channel = await interaction.client.fetch_channel(int(listing["channel_id"]))
                message = await channel.fetch_message(int(listing["message_id"]))
                await message.edit(embed=build_land_embed(self.land_id),
                                    view=land_card_view(listing))
            except discord.HTTPException:
                pass  # the bid itself already succeeded; a stale card is cosmetic


class LandCardView(discord.ui.View):
    """Persistent. Registered once at boot with no listing id attached --
    every callback re-resolves the listing from `interaction.message`."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Bid", style=discord.ButtonStyle.primary,
                        custom_id="nola:land:bid")
    async def bid_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        land_id = parse_land_id(interaction.message)
        if land_id is None:
            await interaction.response.send_message("Could not identify this listing.", ephemeral=True)
            return
        listing = queries.get_land_detail(land_id)
        if listing is None or listing["status"] != "open":
            await interaction.response.send_message("This listing is no longer open.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _BidAmountModal(land_id, listing["name"], money.user(interaction.user.id))
        )
