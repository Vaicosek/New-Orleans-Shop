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

from core import loans as loans_core
from core import loyalty, money

from .. import queries
from .pickers import OptionPickerView, UserPickerView
from ..ui.embed import SEP, money_text, panel_embed, rows


def build_wallet_embed(subject: str) -> discord.Embed:
    bal = money.balance(subject)
    loy = loyalty.summary(subject)
    lines = [
        f"Balance: {money_text(bal.coins)}",
        f"Held: {money_text(bal.held)}",
        f"Available: {money_text(bal.available)}",
        "",
        f"Rank: **{loy['tier']['name']}**" + (" (set by staff)" if loy["overridden"] else ""),
    ]
    if loy["payout_bonus_pct"] or loy["bet_bonus_pct"]:
        lines.append(
            f"+{loy['payout_bonus_pct']}% on order payouts, "
            f"+{loy['bet_bonus_pct']}% higher casino bet limits"
        )
    if loy["next_tier"] is not None:
        lines.append(
            f"{loy['next_tier']['points_needed']:,} points to {loy['next_tier']['name']}"
        )
    owed = loans_core.outstanding_owed(subject)
    if owed:
        limit = loans_core.credit_limit_for(subject)
        lines.append(f"Owed on loans: {money_text(owed)} (limit {money_text(limit)})")
    return panel_embed("Your wallet", rows(lines))


def _unix(ts: str) -> int:
    """`ledger_entries.ts` is a naive UTC string ("%Y-%m-%d %H:%M:%S") --
    stamp it UTC explicitly before converting, or a naive `datetime` reads
    as local time and every timestamp in the history is wrong by the
    server's UTC offset."""
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _describe_entry(e: dict) -> str:
    """Turn a ledger_entries row into a human sentence from its STRUCTURED
    fields (ref_kind / ref_id / service), never the free-text `reason`.

    `reason` is meant for logs/audits and carries raw internal ids verbatim
    (e.g. "coinflip win, round <round_id>", "market <market_id> resolved",
    "order #<id> payout") -- see core/games.py, core/predictions.py and
    core/orders.py for what actually gets passed as reason=. None of that
    belongs in a player-facing panel. Only fall back to the raw reason when
    ref_kind doesn't map to anything we're confident about.
    """
    ref_kind = e.get("ref_kind")
    delta = e["delta"]
    if ref_kind == "game_round":
        return "Casino win" if delta > 0 else "Casino loss"
    if ref_kind == "pred_market":
        kind = "payout" if delta > 0 else "stake"
        ref_id = e.get("ref_id")
        question = None
        if ref_id is not None:
            try:
                market = queries.get_market_detail(int(ref_id))
            except (TypeError, ValueError):
                market = None
            if market is not None:
                question = market.get("question")
        if question:
            return f"Prediction market {kind}: {question}"[:100]
        return f"Prediction market {kind}"
    if ref_kind == "order":
        ref_id = e.get("ref_id")
        return f"Order #{ref_id} payout" if ref_id else "Order payout"
    if ref_kind == "treasury":
        return "Treasury funding"
    if ref_kind is None and e.get("service") == "owner":
        return "Wallet transfer received" if delta > 0 else "Wallet transfer sent"
    # Unrecognised shape -- fall back to the raw reason rather than guess.
    return str(e["reason"])


def build_history_embed(subject: str) -> discord.Embed:
    entries = money.history(subject, limit=15)
    lines = [
        f"<t:{_unix(e['ts'])}:f>  {'+' if e['delta'] > 0 else ''}{money_text(e['delta'])}  "
        f"{SEP} {_describe_entry(e)}"
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


class _BorrowAmountModal(discord.ui.Modal):
    """How much to borrow -- a quantity, capped against the borrower's flat
    credit limit at submit time (the real check is still in
    core/loans.py's `borrow`; this is just an early, friendlier refusal)."""

    def __init__(self, borrower_id: int) -> None:
        super().__init__(title="Borrow", timeout=300)
        self.borrower_id = borrower_id
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

        subject = money.user(self.borrower_id)
        limit = loans_core.credit_limit_for(subject)
        owed = loans_core.outstanding_owed(subject)
        if owed + amount > limit:
            await interaction.followup.send(
                f"You already owe {money_text(owed)}; your limit is {money_text(limit)}. "
                f"That leaves {money_text(max(limit - owed, 0))} you can still borrow.",
                ephemeral=True,
            )
            return

        interest = (amount * loans_core.LOAN_INTEREST_PCT) // 100
        idem_key = money.new_event_id("loan.borrow")
        await interaction.followup.send(
            f"Borrow {money_text(amount)}? You will owe {money_text(amount + interest)} "
            f"({loans_core.LOAN_INTEREST_PCT}% flat interest) within "
            f"{loans_core.LOAN_TERM_DAYS} days.",
            view=_BorrowConfirmGate(self.borrower_id, amount, idem_key),
            ephemeral=True,
        )


class _BorrowConfirmGate(discord.ui.View):
    def __init__(self, borrower_id: int, amount: int, idem_key: str) -> None:
        super().__init__(timeout=120)
        self.borrower_id, self.amount, self.idem_key = borrower_id, amount, idem_key
        self._submitted = False

    @discord.ui.button(label="Confirm borrow", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if self._submitted:
            await interaction.response.send_message("That loan was already submitted.",
                                                      ephemeral=True)
            return
        self._submitted = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        subject = money.user(self.borrower_id)
        try:
            with money.guarded(self.idem_key, service="shop", endpoint="loan.borrow",
                                payload={"subject": subject, "amount": self.amount}) as g:
                if g.replay:
                    result = g.response
                else:
                    result = loans_core.borrow(subject, self.amount, conn=g.conn)
                    g.set_response(result)
        except loans_core.LoanError as err:
            self._submitted = False
            button.disabled = False
            await interaction.followup.send(f"Could not borrow that: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not borrow that: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Borrowed {money_text(self.amount)} (loan #{result['loan_id']}). "
            f"You owe {money_text(result['owed'])} within {loans_core.LOAN_TERM_DAYS} days.",
            ephemeral=True,
        )


class _RepayAmountModal(discord.ui.Modal):
    """The loan itself is already resolved by the picker that opens this;
    the amount is free text, clamped server-side to what is actually owed."""

    def __init__(self, borrower_id: int, loan: dict) -> None:
        remaining = loan["principal"] + loan["interest"] - loan["paid"]
        super().__init__(title=f"Repay loan #{loan['id']}"[:45], timeout=300)
        self.borrower_id, self.loan_id, self.remaining = borrower_id, loan["id"], remaining
        self.amount = discord.ui.TextInput(
            label=f"Amount (g) -- {remaining:,} owed", placeholder=f"e.g. {remaining}",
            max_length=10, required=True,
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

        subject = money.user(self.borrower_id)
        bal = money.balance(subject)
        if amount > bal.available:
            await interaction.followup.send(
                f"You only have {money_text(bal.available)} available.", ephemeral=True)
            return

        idem_key = money.new_event_id("loan.repay")
        pay = min(amount, self.remaining)
        await interaction.followup.send(
            f"Repay {money_text(pay)} on loan #{self.loan_id}?"
            + (" This pays it off in full." if pay >= self.remaining else
               f" {money_text(self.remaining - pay)} would remain."),
            view=_RepayConfirmGate(self.borrower_id, self.loan_id, amount, idem_key),
            ephemeral=True,
        )


class _RepayConfirmGate(discord.ui.View):
    def __init__(self, borrower_id: int, loan_id: int, amount: int, idem_key: str) -> None:
        super().__init__(timeout=120)
        self.borrower_id, self.loan_id = borrower_id, loan_id
        self.amount, self.idem_key = amount, idem_key
        self._submitted = False

    @discord.ui.button(label="Confirm repayment", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if self._submitted:
            await interaction.response.send_message("That repayment was already submitted.",
                                                      ephemeral=True)
            return
        self._submitted = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        subject = money.user(self.borrower_id)
        try:
            with money.guarded(self.idem_key, service="shop", endpoint="loan.repay",
                                payload={"loan": self.loan_id, "amount": self.amount}) as g:
                if g.replay:
                    result = g.response
                else:
                    result = loans_core.repay(self.loan_id, subject, self.amount, conn=g.conn)
                    g.set_response(result)
        except loans_core.LoanError as err:
            self._submitted = False
            button.disabled = False
            await interaction.followup.send(f"Could not repay that: {err}", ephemeral=True)
            return
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not repay that: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Paid {money_text(result['paid_this_time'])} on loan #{self.loan_id}."
            + (" Paid in full." if result["status"] == "repaid"
               else f" {money_text(result['remaining'])} remaining."),
            ephemeral=True,
        )


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

    @discord.ui.button(label="Borrow", style=discord.ButtonStyle.secondary)
    async def borrow(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_BorrowAmountModal(self.owner_id))

    @discord.ui.button(label="Repay", style=discord.ButtonStyle.secondary)
    async def repay(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        subject = money.user(self.owner_id)
        loans = queries.list_subject_loans(subject)
        options = [
            (f"#{l['id']}: {l['principal'] + l['interest'] - l['paid']:,}g owed"[:100], str(l["id"]))
            for l in loans
        ]

        async def picked(inter: discord.Interaction, loan_id_str: str) -> None:
            if loan_id_str == "_none":
                await inter.response.send_message("You have no open loans.", ephemeral=True)
                return
            loan = queries.get_loan_detail(int(loan_id_str))
            if loan is None or loan["status"] != "open":
                await inter.response.send_message("That loan is no longer open.", ephemeral=True)
                return
            await inter.response.send_modal(_RepayAmountModal(self.owner_id, loan))

        await interaction.response.send_message(
            "Which loan are you repaying?",
            view=OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )
