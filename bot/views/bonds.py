"""Bond board: the persistent public card posted in the bonds channel.

Same shape as bot/views/auctions.py's AuctionCardView / bot/views/land.py's
LandCardView: registered once at boot with no bond baked in, every callback
re-resolves the bond id from `interaction.message`'s embed footer. No
`/bonds` panel -- the card is the whole browsing surface, same reasoning as
auctions and land. Issuing (staff-only) and voiding both live in `/admin`.
"""
from __future__ import annotations

import re
import secrets

import discord

from core import bonds as bonds_core
from core import money

from .. import addressing, queries
from ..ui.embed import SEP, money_text, panel_embed, rows

_ADDRESS_MARK = re.compile(r"address (\S+)")


def _bond_footer(bond_id: int, code: str) -> str:
    return f"address {code}"


def parse_bond_id(message: discord.Message | None) -> int | None:
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
    if found is None or found[0] != "bond":
        return None
    try:
        return int(found[1])
    except (TypeError, ValueError):
        return None


_STATUS_LABELS = {
    "open": "Open",
    "matured": "Matured",
    "voided": "Voided",
}


def _bps_pct(bps: int) -> str:
    """1 bps = 0.01% -- render as a plain percent, at most 2 decimals, no
    trailing zeros (50 bps -> "0.5%", 1000 bps -> "10%")."""
    pct = bps / 100
    text = f"{pct:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def build_bond_embed(bond_id: int) -> discord.Embed:
    bond = queries.get_bond_detail(bond_id)
    if bond is None:
        return panel_embed("Bond not found", "This bond no longer exists.", tone="loss")

    remaining = bond["units_total"] - bond["units_sold"]
    lines = [
        f"{money_text(bond['unit_price'])} per unit {SEP} {remaining:,} of "
        f"{bond['units_total']:,} unit(s) left",
        f"{_bps_pct(bond['coupon_bps'])} coupon every {bond['coupon_interval_days']} "
        f"day(s), {bond['term_days']} day term",
        f"{bond['holder_count']} holder(s)",
    ]
    if bond["status"] == "open":
        lines.append(f"Next coupon: <t:{_epoch(bond['next_coupon_at'])}:R>")
        lines.append(f"Matures: <t:{_epoch(bond['matures_at'])}:R>")
    elif bond["status"] == "matured":
        lines.append("**Matured. Principal has been repaid to every holder.**")
    elif bond["status"] == "voided":
        lines.append("**Voided by staff. Every holder's principal was refunded.**")

    code = addressing.mint("bond", bond_id)
    tone = "gain" if bond["status"] == "matured" else "neutral"
    return panel_embed(
        f"Bond: {bond['name']} ({_STATUS_LABELS.get(bond['status'], bond['status'])})",
        rows(lines),
        footer=_bond_footer(bond_id, code),
        tone=tone,
    )


def _epoch(sql_ts: str) -> int:
    """Same conversion as bot/views/auctions.py's `_epoch`."""
    from datetime import datetime, timezone
    dt = datetime.strptime(sql_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def bond_card_view(bond: dict) -> "BondCardView":
    """Same reasoning as `auction_card_view`/`land_card_view`: reconstructed
    from a fresh read every refresh, buttons carry no per-bond state."""
    view = BondCardView()
    if bond["status"] != "open":
        for child in view.children:
            child.disabled = True
    return view


class _BuyUnitsModal(discord.ui.Modal):
    """How many units -- a quantity. The bond itself is already resolved
    from the card's message before this opens."""

    def __init__(self, bond_id: int, bond_name: str, subject: str) -> None:
        super().__init__(title=f"Buy: {bond_name}"[:45], timeout=300)
        self.bond_id, self.subject = bond_id, subject
        self.units = discord.ui.TextInput(
            label="Units", placeholder="e.g. 5", max_length=8, required=True
        )
        self.add_item(self.units)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            units = int(str(self.units.value).strip())
            if units <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("Units must be a positive whole number.", ephemeral=True)
            return

        bond = queries.get_bond_detail(self.bond_id)
        if bond is None or bond["status"] != "open":
            await interaction.followup.send("This bond is no longer open.", ephemeral=True)
            return
        remaining = bond["units_total"] - bond["units_sold"]
        if units > remaining:
            await interaction.followup.send(
                f"Only {remaining:,} unit(s) left.", ephemeral=True)
            return

        cost = bond["unit_price"] * units
        bal = money.balance(self.subject)
        if cost > bal.available:
            await interaction.followup.send(
                f"That costs {money_text(cost)}; you only have "
                f"{money_text(bal.available)} available.", ephemeral=True)
            return

        preview_id = secrets.token_hex(8)
        idem_key = f"bond.buy:{self.bond_id}:{units}:{preview_id}"
        await interaction.followup.send(
            f"Buy {units} unit(s) of \"{bond['name']}\" for {money_text(cost)}? "
            f"{_bps_pct(bond['coupon_bps'])} coupon every {bond['coupon_interval_days']} "
            f"day(s), matures in {bond['term_days']} day(s).",
            view=_BuyConfirmGate(self.bond_id, bond["name"], self.subject, units, cost,
                                  idem_key=idem_key),
            ephemeral=True,
        )


class _BuyConfirmGate(discord.ui.View):
    """Confirm-or-walk-away for one previewed purchase. Same double-click
    guard as auctions'/land's bid confirm gates."""

    def __init__(self, bond_id: int, bond_name: str, subject: str, units: int, cost: int, *,
                 idem_key: str) -> None:
        super().__init__(timeout=120)
        self.bond_id, self.bond_name, self.subject = bond_id, bond_name, subject
        self.units, self.cost = units, cost
        self.idem_key = idem_key
        self._submitted = False

    @discord.ui.button(label="Confirm purchase", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if self._submitted:
            await interaction.response.send_message("That purchase was already submitted.",
                                                      ephemeral=True)
            return
        self._submitted = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            with money.guarded(self.idem_key, service="shop", endpoint="bond.buy",
                                payload={"bond": self.bond_id, "units": self.units}) as g:
                if not g.replay:
                    result = bonds_core.buy(self.bond_id, self.subject, self.units, conn=g.conn)
                    g.set_response(result)
        except bonds_core.BondError as err:
            self._submitted = False
            button.disabled = False
            await interaction.followup.send(f"Could not buy that: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not buy that: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Bought {self.units} unit(s) of \"{self.bond_name}\" for {money_text(self.cost)}.",
            ephemeral=True,
        )

        bond = queries.get_bond_detail(self.bond_id)
        if bond and bond.get("channel_id") and bond.get("message_id"):
            try:
                channel = interaction.client.get_channel(int(bond["channel_id"]))
                if channel is None:
                    channel = await interaction.client.fetch_channel(int(bond["channel_id"]))
                message = await channel.fetch_message(int(bond["message_id"]))
                await message.edit(embed=build_bond_embed(self.bond_id),
                                    view=bond_card_view(bond))
            except discord.HTTPException:
                pass  # the purchase itself already succeeded; a stale card is cosmetic


class BondCardView(discord.ui.View):
    """Persistent. Registered once at boot with no bond id attached --
    every callback re-resolves the bond from `interaction.message`."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Buy units", style=discord.ButtonStyle.primary,
                        custom_id="nola:bond:buy")
    async def buy_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        bond_id = parse_bond_id(interaction.message)
        if bond_id is None:
            await interaction.response.send_message("Could not identify this bond.", ephemeral=True)
            return
        bond = queries.get_bond_detail(bond_id)
        if bond is None or bond["status"] != "open":
            await interaction.response.send_message("This bond is no longer open.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _BuyUnitsModal(bond_id, bond["name"], money.user(interaction.user.id))
        )
