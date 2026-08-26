"""Wallet panel: balance, held, history, transfer (UserSelect -> modal).

Transfer is money-moving and irreversible once it lands, so it gets the full
treatment: a picker for the recipient (never a typed id), a preview with the
real numbers, and a typed confirmation -- the recipient's display NAME, not
their id -- before `core.money.transfer` actually runs.
"""
from __future__ import annotations

import discord

from core import money

from .pickers import UserPickerView
from ..ui.embed import money_text, panel_embed, rows


def build_wallet_embed(subject: str, currency_name: str) -> discord.Embed:
    bal = money.balance(subject)
    body = (
        f"Balance: {money_text(bal.coins, currency_name)}\n"
        f"Held (in open bets, stakes or orders): {money_text(bal.held, currency_name)}\n"
        f"Available: {money_text(bal.available, currency_name)}"
    )
    return panel_embed("Your wallet", body)


def build_history_embed(subject: str, currency_name: str) -> discord.Embed:
    entries = money.history(subject, limit=15)
    lines = [
        f"{e['ts']}  {'+' if e['delta'] > 0 else ''}{money_text(e['delta'], currency_name)}  "
        f"-- {e['reason']}"
        for e in entries
    ]
    return panel_embed("Recent activity", rows(lines, empty_text="No activity yet."))


class _AmountModal(discord.ui.Modal):
    """The amount is genuinely free text (a quantity); the recipient is
    already resolved by the UserSelect that opened this."""

    def __init__(self, sender_id: int, recipient: discord.abc.User, currency_name: str):
        super().__init__(title=f"Send to {recipient.display_name}"[:45], timeout=300)
        self.sender_id = sender_id
        self.recipient = recipient
        self.currency_name = currency_name
        self.amount = discord.ui.TextInput(
            label=f"Amount ({currency_name}s)", placeholder="e.g. 100", max_length=10, required=True
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

        sender = money.user(self.sender_id)
        bal = money.balance(sender)
        if amount > bal.available:
            await interaction.followup.send(
                f"You only have {money_text(bal.available, self.currency_name)} available.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Send {money_text(amount, self.currency_name)} to {self.recipient.display_name}? "
            f"Your available balance would drop to "
            f"{money_text(bal.available - amount, self.currency_name)}.",
            view=_TransferConfirmGate(self.sender_id, self.recipient, amount, self.currency_name),
            ephemeral=True,
        )


class _ConfirmNameModal(discord.ui.Modal):
    def __init__(self, sender_id: int, recipient: discord.abc.User, amount: int, currency_name: str):
        super().__init__(title="Confirm transfer", timeout=300)
        self.sender_id = sender_id
        self.recipient = recipient
        self.amount = amount
        self.currency_name = currency_name
        self.confirm = discord.ui.TextInput(
            label=f"Type the recipient's name: {recipient.display_name}",
            placeholder=recipient.display_name, max_length=100, required=True,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.recipient.display_name.strip().lower():
            await interaction.followup.send("Name didn't match -- transfer cancelled.", ephemeral=True)
            return
        sender = money.user(self.sender_id)
        recipient_subject = money.user(self.recipient.id)
        try:
            money.transfer(
                sender, recipient_subject, self.amount, service="owner",
                reason=f"wallet transfer to {self.recipient.display_name}",
            )
        except money.MoneyError as err:
            await interaction.followup.send(f"Transfer failed: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Sent {money_text(self.amount, self.currency_name)} to {self.recipient.display_name}.",
            ephemeral=True,
        )


class _TransferConfirmGate(discord.ui.View):
    def __init__(self, sender_id: int, recipient: discord.abc.User, amount: int, currency_name: str):
        super().__init__(timeout=120)
        self.sender_id, self.recipient = sender_id, recipient
        self.amount, self.currency_name = amount, currency_name

    @discord.ui.button(label="Confirm transfer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            _ConfirmNameModal(self.sender_id, self.recipient, self.amount, self.currency_name)
        )


class WalletPanelView(discord.ui.View):
    def __init__(self, owner_id: int, currency_name: str) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.currency_name = currency_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="History", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = build_history_embed(money.user(interaction.user.id), self.currency_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.primary)
    async def transfer(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, member: discord.abc.User) -> None:
            if member.id == self.owner_id:
                await inter.response.send_message("You can't transfer to yourself.", ephemeral=True)
                return
            await inter.response.send_modal(
                _AmountModal(self.owner_id, member, self.currency_name)
            )

        await interaction.response.send_message(
            "Who are you sending coins to?",
            view=UserPickerView(self.owner_id, picked),
            ephemeral=True,
        )
