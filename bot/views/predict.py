"""Prediction markets panel: open markets, stake, my positions.

Pari-mutuel, Discord-only per CONTRACT.md section 1. Staking places a hold
and is previewed before it commits; resolving a market (staff-only,
irreversible) lives in the admin panel, not here.
"""
from __future__ import annotations

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
    are already resolved by the two pickers before this opens."""

    def __init__(self, market: dict, outcome: str, subject: str):
        super().__init__(title=f"Stake: {market['question']}"[:45], timeout=300)
        self.market, self.outcome, self.subject = market, outcome, subject
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

        await interaction.followup.send(
            f"Stake {money_text(amount)} on \"{self.outcome}\" in "
            f"\"{self.market['question']}\"? This places a hold until the market resolves or voids.",
            view=_StakeConfirmGate(self.market, self.outcome, self.subject, amount),
            ephemeral=True,
        )


class _StakeConfirmGate(discord.ui.View):
    def __init__(self, market: dict, outcome: str, subject: str, amount: int):
        super().__init__(timeout=120)
        self.market, self.outcome, self.subject = market, outcome, subject
        self.amount = amount

    @discord.ui.button(label="Confirm stake", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        from core import predictions

        try:
            predictions.stake(self.market["id"], self.subject, self.outcome, self.amount)
        except predictions.MarketError as err:
            await interaction.followup.send(f"Could not place that stake: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not place that stake: {err}", ephemeral=True)
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
                    _StakeAmountModal(market, outcome, money.user(self.owner_id))
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
