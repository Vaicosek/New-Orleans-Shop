"""Wallet panel: balance, held, history, transfer (UserSelect -> modal).

Transfer is money-moving and irreversible once it lands, so it gets the full
treatment: a picker for the recipient (never a typed id), a preview with the
real numbers, and a typed confirmation -- the recipient's display NAME, not
their id -- before `core.money.transfer` actually runs.

The preview mints an idempotency key at the SAME time it computes the
numbers it shows, and that one key rides along through the confirm button
and the name modal to `money.guarded`. A double-click on "Confirm transfer",
or the name modal being submitted twice, replays the same key: the second
attempt is a no-op replay inside `money.guarded`, never a second transfer.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from core import money

from .pickers import UserPickerView
from ..ui.embed import SEP, money_text, panel_embed, rows


def build_wallet_embed(subject: str) -> discord.Embed:
    bal = money.balance(subject)
    body = (
        f"Balance: {money_text(bal.coins)}\n"
        f"Held: {money_text(bal.held)}\n"
        f"Available: {money_text(bal.available)}"
    )
    return panel_embed("Your wallet", body)


def _unix(ts: str) -> int:
    """`ledger_entries.ts` is a naive UTC string ("%Y-%m-%d %H:%M:%S") --
    stamp it UTC explicitly before converting, or a naive `datetime` reads
    as local time and every timestamp in the history is wrong by the
    server's UTC offset."""
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def build_history_embed(subject: str) -> discord.Embed:
    entries = money.history(subject, limit=15)
    lines = [
        f"<t:{_unix(e['ts'])}:f>  {'+' if e['delta'] > 0 else ''}{money_text(e['delta'])}  "
        f"{SEP} {e['reason']}"
        for e in entries
    ]
    return panel_embed("Recent activity", rows(lines, empty_text="No activity yet."))


class _AmountModal(discord.ui.Modal):
    """The amount is genuinely free text (a quantity); the recipient is
    already resolved by the UserSelect that opened this."""

    def __init__(self, sender_id: int, recipient: discord.abc.User):
        super().__init__(title=f"Send to {recipient.display_name}"[:45], timeout=300)
        self.sender_id = sender_id
        self.recipient = recipient
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

        sender = money.user(self.sender_id)
        bal = money.balance(sender)
        if amount > bal.available:
            await interaction.followup.send(
                f"You only have {money_text(bal.available)} available.",
                ephemeral=True,
            )
            return

        # Minted WITH the preview, not at send-time -- this is the one
        # request the preview describes, and every retry of it (double
        # click, resubmitted modal) carries this same key.
        idem_key = money.new_event_id("wallet.transfer")
        await interaction.followup.send(
            f"Send {money_text(amount)} to {self.recipient.display_name}? "
            f"Your available balance would drop to {money_text(bal.available - amount)}.",
            view=_TransferConfirmGate(self.sender_id, self.recipient, amount, idem_key),
            ephemeral=True,
        )


class _ConfirmNameModal(discord.ui.Modal):
    def __init__(self, sender_id: int, recipient: discord.abc.User, amount: int, idem_key: str):
        super().__init__(title="Confirm transfer", timeout=300)
        self.sender_id = sender_id
        self.recipient = recipient
        self.amount = amount
        self.idem_key = idem_key
        self.confirm = discord.ui.TextInput(
            label=f"Type the recipient's name: {recipient.display_name}"[:45],
            placeholder="Type it exactly as shown above", max_length=100, required=True,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.recipient.display_name.strip().lower():
            await interaction.followup.send(f"Name didn't match {SEP} transfer cancelled.", ephemeral=True)
            return
        sender = money.user(self.sender_id)
        recipient_subject = money.user(self.recipient.id)
        payload = {"sender": sender, "recipient": recipient_subject, "amount": self.amount}
        try:
            with money.guarded(self.idem_key, service="owner", endpoint="wallet.transfer",
                                payload=payload) as guard:
                if not guard.replay:
                    money.transfer(
                        sender, recipient_subject, self.amount, service="owner",
                        reason=f"wallet transfer to {self.recipient.display_name}",
                        idem_key=self.idem_key, conn=guard.conn,
                    )
                    guard.set_response({"sent": self.amount})
        except money.MoneyError as err:
            await interaction.followup.send(f"Transfer failed: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Sent {money_text(self.amount)} to {self.recipient.display_name}.",
            ephemeral=True,
        )


class _TransferConfirmGate(discord.ui.View):
    def __init__(self, sender_id: int, recipient: discord.abc.User, amount: int, idem_key: str):
        super().__init__(timeout=120)
        self.sender_id, self.recipient = sender_id, recipient
        self.amount, self.idem_key = amount, idem_key

    @discord.ui.button(label="Confirm transfer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Disable on first use -- the idempotency key is the real guard
        # against a duplicate SEND, this is just so the button itself
        # doesn't invite a second click while the first is in flight.
        button.disabled = True
        await interaction.response.send_modal(
            _ConfirmNameModal(self.sender_id, self.recipient, self.amount, self.idem_key)
        )
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass


class WalletPanelView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="History", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = build_history_embed(money.user(interaction.user.id))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.primary)
    async def transfer(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, member: discord.abc.User) -> None:
            if member.id == self.owner_id:
                await inter.response.send_message("You can't transfer to yourself.", ephemeral=True)
                return
            await inter.response.send_modal(_AmountModal(self.owner_id, member))

        await interaction.response.send_message(
            "Who are you sending gold to?",
            view=UserPickerView(self.owner_id, picked),
            ephemeral=True,
        )
