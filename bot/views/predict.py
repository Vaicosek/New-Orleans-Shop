"""Prediction markets panel: open markets, stake, my positions.

Pari-mutuel, Discord-only per CONTRACT.md section 1. Staking places a hold
and is previewed before it commits; resolving a market (staff-only,
irreversible) lives in the admin panel, not here.
"""
from __future__ import annotations

import secrets

import discord

from core import money

from . import pickers
from ..ui.embed import SEP, money_text, panel_embed, rows
from .. import queries


def build_markets_embed() -> discord.Embed:
    markets = queries.list_open_markets(limit=15)
    lines = [
        f"{m['question']}  {SEP} pool {money_text(m['pool'])}  {SEP} "
        f"outcomes: {', '.join(o['label'] for o in m['outcomes'])}"
        for m in markets
    ]
    return panel_embed("Open prediction markets", rows(lines, empty_text="No open markets."))


def build_positions_embed(subject: str) -> discord.Embed:
    stakes = queries.list_user_stakes(subject)
    lines = []
    for s in stakes:
        state = s["market_status"]
        payout_text = ""
        if s["payout_coins"] is not None:
            payout_text = f"  {SEP} paid {money_text(s['payout_coins'])}"
        lines.append(
            f"{s['question']}  {SEP} {s['outcome_label']}  {SEP} staked "
            f"{money_text(s['amount'])}  ({state}){payout_text}"
        )
    return panel_embed("Your positions", rows(lines, empty_text="No positions yet."))


class _StakeAmountModal(discord.ui.Modal):
    """The stake amount is free text (a quantity); the market and outcome
    are already resolved by the two pickers before this opens.

    This is also where the stake's idempotency key is minted. The preview is
    the SOURCE EVENT: one preview means one key, which means at most one
    stake no matter how many times the confirm button is clicked. The key is
    built from the market, the outcome id, the amount and a fresh random
    preview id -- never from a timestamp, which is not stable across a
    re-read and would let a double click mint two keys for one intent.
    """

    def __init__(self, market: dict, outcome: str, subject: str, *,
                 outcome_id: int | str | None = None, owner_id: int | None = None):
        super().__init__(title=f"Stake: {market['question']}"[:45], timeout=300)
        self.market, self.outcome, self.subject = market, outcome, subject
        # The label is what predictions.stake takes; the id is what the
        # idempotency key is built from (short, stable, never re-typed).
        self.outcome_id = outcome_id
        self.owner_id = owner_id
        self.amount = discord.ui.TextInput(
            label="Amount (g)", placeholder="e.g. 100", max_length=10, required=True
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
                f"You only have {money_text(bal.available)} available.",
                ephemeral=True,
            )
            return

        # Minted HERE, once, at the preview. Carried unchanged onto the gate.
        preview_id = secrets.token_hex(8)
        idem_key = (f"pred.stake:{self.market['id']}:{self.outcome_id}:"
                    f"{amount}:{preview_id}")

        owner_id = self.owner_id
        if owner_id is None:
            owner_id = getattr(interaction.user, "id", None)

        await interaction.followup.send(
            f"Stake {money_text(amount)} on \"{self.outcome}\" in "
            f"\"{self.market['question']}\"? This places a hold until the market resolves or voids.",
            view=_StakeConfirmGate(self.market, self.outcome, self.subject, amount,
                                   outcome_id=self.outcome_id, owner_id=owner_id,
                                   idem_key=idem_key),
            ephemeral=True,
        )


class _StakeConfirmGate(discord.ui.View):
    """Confirm-or-walk-away for one previewed stake.

    Two things stop a double click becoming a double hold:

      1. the button disables itself on the FIRST click, in the same
         interaction response that acks it (`edit_message` is a valid ack and
         lands well inside the 3-second window), and `_submitted` refuses a
         click that races past the edit;
      2. the placement runs under `money.guarded` on the preview-derived
         idempotency key, so even two clicks that both get through resolve to
         the SAME stake -- the second one replays the first one's stake id
         instead of placing a second hold.

    Belt and braces on purpose: (1) is a UI nicety that a lagging client can
    defeat, (2) is the guarantee.
    """

    def __init__(self, market: dict, outcome: str, subject: str, amount: int, *,
                 outcome_id: int | str | None = None, owner_id: int | None = None,
                 idem_key: str | None = None):
        super().__init__(timeout=120)
        self.market, self.outcome, self.subject = market, outcome, subject
        self.amount = amount
        self.outcome_id = outcome_id
        self.owner_id = owner_id
        self.idem_key = idem_key or (
            f"pred.stake:{market['id']}:{outcome_id}:{amount}:{secrets.token_hex(8)}")
        self._submitted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Re-checked on EVERY click: an ephemeral panel message is still a
        # message, and anyone who can see it can address its components.
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "That stake preview isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm stake", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        if self._submitted:
            await interaction.response.send_message(
                "That stake was already submitted.", ephemeral=True)
            return
        self._submitted = True
        button.disabled = True
        # edit_message IS the ack -- it satisfies the 3-second window and
        # disables the button in one response. Everything after this point
        # must use followup.send.
        await interaction.response.edit_message(view=self)

        from core import predictions

        try:
            with money.guarded(self.idem_key, service="games",
                               endpoint="predictions.stake",
                               payload={"market": self.market["id"],
                                        "outcome_id": self.outcome_id,
                                        "amount": self.amount}) as g:
                if g.replay:
                    stake_id = g.response["stake_id"] if g.response else None
                else:
                    stake_id = predictions.stake(
                        self.market["id"], self.subject, self.outcome,
                        self.amount, conn=g.conn)
                    g.set_response({"stake_id": stake_id})
        except predictions.MarketError as err:
            self._submitted = False
            button.disabled = False
            await interaction.followup.send(
                f"Could not place that stake: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            # Covers IdempotencyInProgress / Conflict / Unresolved as well as
            # InsufficientFunds and GamblingBlocked. Never leave a click
            # unanswered.
            await interaction.followup.send(
                f"Could not place that stake: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Staked {money_text(self.amount)} on \"{self.outcome}\".",
            ephemeral=True,
        )


class PredictPanelView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stake", style=discord.ButtonStyle.primary)
    async def stake_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        markets = queries.list_open_markets(limit=25)
        options = [(m["question"][:100], str(m["id"])) for m in markets]

        async def market_picked(inter: discord.Interaction, market_id_str: str) -> None:
            if market_id_str == "_none":
                await inter.response.send_message("No open markets.", ephemeral=True)
                return
            market = queries.get_market_detail(int(market_id_str))
            # Keyed on the outcome's id, never its label text -- an outcome
            # label can be long or contain characters that make a poor
            # Select value; the id is always short and stable.
            by_id = {str(o["id"]): o["label"] for o in market["outcomes"]}
            outcome_options = [(o["label"][:100], str(o["id"])) for o in market["outcomes"]]

            async def outcome_picked(inter2: discord.Interaction, outcome_id_str: str) -> None:
                outcome = by_id.get(outcome_id_str)
                if outcome is None:
                    await inter2.response.send_message("That outcome no longer exists.", ephemeral=True)
                    return
                await inter2.response.send_modal(
                    _StakeAmountModal(market, outcome, money.user(self.owner_id),
                                      outcome_id=outcome_id_str,
                                      owner_id=self.owner_id)
                )

            await inter.response.send_message(
                f"Pick an outcome for \"{market['question']}\":",
                view=pickers.OptionPickerView(self.owner_id, outcome_options, outcome_picked),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a market to stake on:",
            view=pickers.OptionPickerView(self.owner_id, options, market_picked),
            ephemeral=True,
        )

    @discord.ui.button(label="My positions", style=discord.ButtonStyle.secondary)
    async def positions(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = build_positions_embed(money.user(self.owner_id))
        await interaction.followup.send(embed=embed, ephemeral=True)
