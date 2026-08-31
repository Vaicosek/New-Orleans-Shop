"""Admin panel: items, prices, thresholds, resolve markets, treasury.

Staff-only, gated by `interaction_check` on every button, re-checked on
every click rather than assumed once at panel-open time. Every destructive
or money-deciding action here previews real figures first; resolving or
voiding a market is irreversible and requires typing back a NAME (the
chosen outcome, or the market's own question) -- never an id.
"""
from __future__ import annotations

import secrets

import discord

from core import alerts, audit, catalog, money, predictions, pricing

from .. import addressing

from . import pickers
from .. import permissions
from ..permissions import is_staff
from ..ui.embed import money_text, panel_embed, price_line, rows
from .. import queries


class _StaffGatedView(discord.ui.View):
    def __init__(self, owner_id: int, config, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.config = config

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        if not is_staff(interaction.user, self.config):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return False
        return True


def build_admin_embed(config) -> discord.Embed:
    items = catalog.list_items(active_only=True)
    # Staff view: every declared category, including a planned one with
    # nothing stocked yet -- that emptiness IS the to-do list, so it stays
    # visible here even though the public storefront hides it.
    cats = catalog.categories_with_items(active_only=True, include_empty=True)
    cat_lines = []
    for c in cats:
        if not c["items"]:
            cat_lines.append(f"{c['name']}: planned, no items yet")
            continue
        # Sub-groups (e.g. Wood's Logs/Leaves/Saplings) shown inline so
        # staff can see the same split the owner's price sheet has, without
        # opening the item picker.
        groups = c["groups"]
        if len(groups) == 1 and not groups[0]["subcategory"]:
            cat_lines.append(f"{c['name']}: {len(c['items'])} item(s)")
        else:
            sub_bits = ", ".join(
                f"{g['subcategory'] or 'other'} {len(g['items'])}" for g in groups
            )
            cat_lines.append(f"{c['name']}: {len(c['items'])} item(s) ({sub_bits})")
    body = f"{len(items)} active item(s). Staff-only actions below.\n\n" + rows(cat_lines)
    return panel_embed("Admin", body, tone="brand")


def build_treasury_embed() -> discord.Embed:
    """Balances by their real names. `treasury:shop` is plumbing and never
    appears in a surface a person reads."""
    lines = []
    for subject, label in money.TREASURY_NAMES.items():
        bal = money.balance(subject)
        held = f"  ({money_text(bal.held)} held)" if bal.held else ""
        lines.append(f"{label}: {money_text(bal.coins)}{held}")
    return panel_embed("Treasury", rows(lines), tone="brand")


class _FundAmountModal(discord.ui.Modal):
    """Free text, because an amount IS free text -- a picker cannot offer
    every number. The treasury itself was chosen from a Select first.

    This is also where the funding's idempotency key is MINTED, at the source
    event, from a fresh `preview_id`. One preview is one approval is one key:
    the key travels unchanged through the gate and the confirm modal, so a
    double click, a Discord retry or a re-submitted modal all resolve to the
    same single mint. It is never rebuilt later from a timestamp or from an
    interaction id, both of which differ per click and would mint twice.
    """

    def __init__(self, subject: str, funder: str, config):
        super().__init__(title=f"Fund {money.TREASURY_NAMES[subject]}"[:45], timeout=300)
        self.subject, self.funder, self.config = subject, funder, config
        self.amount = discord.ui.TextInput(
            label=f"How much {pricing.CURRENCY} to add?", placeholder="e.g. 50000",
            max_length=15)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        raw = str(self.amount.value).strip().replace(",", "").replace(" ", "")
        if not raw.isdigit() or int(raw) <= 0:
            await interaction.followup.send(
                "Amount must be a positive whole number.", ephemeral=True)
            return
        amount = int(raw)
        label = money.TREASURY_NAMES[self.subject]
        before = money.balance(self.subject).coins
        preview_id = secrets.token_hex(8)
        idem_key = f"treasury.fund:{self.subject}:{amount}:{preview_id}"
        # Preview with the real figures. He confirms numbers, not intentions.
        await interaction.followup.send(
            f"Funding {label}\n"
            f"Now: {money_text(before)}\n"
            f"Adding: {money_text(amount)}\n"
            f"After: {money_text(before + amount)}\n\n"
            f"This creates {money_text(amount)} that did not exist before. "
            f"Type the treasury's name to confirm.",
            view=_FundGate(self.subject, amount, self.funder, idem_key, self.config),
            ephemeral=True,
        )


class _FundConfirmModal(discord.ui.Modal):
    """Typed confirmation, and the typed string is the treasury's NAME.

    The name is shown on the preview above, so this is an attention gate
    rather than a secret -- which is the honest thing for it to be. What it
    is not is a placeholder pre-filled with the answer.

    The mint runs inside `money.guarded`, claim-first, keyed on the key minted
    at the preview -- and that SAME key is the audit row's `action_key`, which
    is UNIQUE with ON CONFLICT DO NOTHING. So a repeat submission can neither
    create coins a second time nor leave a second row that reads like a second
    legitimate funding. `money.mint`'s own `idem_key` only labels the ledger
    row; it does not deduplicate. `guarded` is what does.
    """

    def __init__(self, subject: str, amount: int, funder: str, idem_key: str):
        super().__init__(title="Confirm funding", timeout=300)
        self.subject, self.amount, self.funder = subject, amount, funder
        self.idem_key = idem_key
        self.confirm = discord.ui.TextInput(
            label=f"Type: {money.TREASURY_NAMES[subject]}", max_length=40)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        label = money.TREASURY_NAMES[self.subject]
        if str(self.confirm.value).strip().casefold() != label.casefold():
            await interaction.followup.send("Name did not match. Nothing was funded.",
                                            ephemeral=True)
            return
        try:
            with money.guarded(self.idem_key, service="owner",
                               endpoint="treasury.fund",
                               payload={"subject": self.subject,
                                        "amount": self.amount}) as g:
                replay = g.replay
                if replay:
                    after = g.response["balance_after"]
                else:
                    after = money.mint(
                        self.subject, self.amount, service="owner",
                        reason=f"treasury funding by {self.funder}",
                        ref_kind="treasury", ref_id=self.subject,
                        idem_key=self.idem_key, conn=g.conn)
                    audit.record(
                        g.conn, actor=self.funder, target=self.subject,
                        kind="treasury.fund",
                        summary=f"funded {label} with {self.amount:,} {pricing.CURRENCY}",
                        money_coins=self.amount,
                        ops=[{"op": "transfer", "from": self.subject,
                              "to": "treasury:house", "amount": self.amount,
                              "note": "reverse of a mint: move it out, it cannot be un-created"}],
                        action_key=self.idem_key,
                    )
                    g.set_response({"balance_after": after})
        except money.MoneyError as err:
            await interaction.followup.send(f"Could not fund {label}: {err}", ephemeral=True)
            return
        if replay:
            await interaction.followup.send(
                f"That funding was already applied. {label} is at {money_text(after)}.",
                ephemeral=True)
            return
        await interaction.followup.send(
            f"{label} funded. New balance {money_text(after)}.", ephemeral=True)


class _FundGate(discord.ui.View):
    """One preview, one key, one effect.

    The gate carries the key minted at the preview and CONSUMES itself on the
    first submit, because a modal cannot be opened and the message edited on
    one interaction response -- so the in-memory flag is the real guard and
    the disabled button is the visual echo of it. Even if both are defeated
    (a restart, a raced click), the key behind them makes the second attempt
    a replay rather than a second mint.
    """

    def __init__(self, subject: str, amount: int, funder: str, idem_key: str,
                 config) -> None:
        super().__init__(timeout=120)
        self.subject, self.amount, self.funder = subject, amount, funder
        self.idem_key = idem_key
        self.config = config
        self._consumed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Re-checked on EVERY click: an ephemeral message is still a message,
        # and owner-ship at panel-open time is not owner-ship now.
        config = self.config or getattr(interaction.client, "nola_config", None)
        if config is None or not permissions.is_owner(interaction.user, config):
            await interaction.response.send_message("Owners only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm funding", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._consumed:
            await interaction.response.send_message(
                "That funding was already submitted.", ephemeral=True)
            return
        self._consumed = True
        button.disabled = True
        await interaction.response.send_modal(
            _FundConfirmModal(self.subject, self.amount, self.funder, self.idem_key))
        try:
            await interaction.message.edit(view=self)      # best-effort visual
        except (discord.HTTPException, AttributeError):
            pass


class _AddItemModal(discord.ui.Modal, title="Add item"):
    """Five inputs, Discord's maximum, and no collapsing.

    `price_unit_pieces` (how many pieces the price buys) and `stack_size` (the
    Minecraft stack) are TWO numbers and are asked for separately. Collapsing
    them made the sapling -- 1 g per 32 pieces, stack size 64 -- impossible to
    enter, and every full stack of it silently half priced. CONTRACT.md
    section 5 split those columns to make exactly that unrepresentable.
    """

    name = discord.ui.TextInput(label="Name", max_length=100)
    price = discord.ui.TextInput(label="Price (g)", max_length=10)
    unit_pieces = discord.ui.TextInput(label="Pieces that price buys", default="64",
                                       max_length=6)
    stack_size = discord.ui.TextInput(label="Stack size", default="64", max_length=6)
    barrel_slots = discord.ui.TextInput(label="Barrel slots", default="54", max_length=6)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            # `catalog.add_item` owns the rule that a price unit may not
            # exceed a stack, and the schema CHECKs it too -- so surface its
            # message rather than re-deriving the rule here and drifting.
            item_id = catalog.add_item(
                str(self.name.value).strip(),
                int(str(self.price.value).strip()),
                price_unit_pieces=int(str(self.unit_pieces.value).strip()),
                stack_size=int(str(self.stack_size.value).strip()),
                barrel_slots=int(str(self.barrel_slots.value).strip()),
            )
        except (catalog.CatalogError, ValueError) as err:
            await interaction.followup.send(f"Could not add item: {err}", ephemeral=True)
            return
        item = catalog.get_item(item_id)
        await interaction.followup.send(
            f"Added {price_line(item['name'], item['price_coins'], item['price_unit_pieces'], item['stack_size'])}",
            ephemeral=True,
        )


class _RepriceModal(discord.ui.Modal):
    def __init__(self, item: dict):
        super().__init__(title=f"Reprice: {item['name']}"[:45], timeout=300)
        self.item = item
        self.price = discord.ui.TextInput(
            label="New price (g)", placeholder=str(item["price_coins"]),
            max_length=10,
        )
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            new_price = int(str(self.price.value).strip())
            catalog.update_item(self.item["id"], price_coins=new_price)
        except (catalog.CatalogError, ValueError) as err:
            await interaction.followup.send(f"Could not reprice: {err}", ephemeral=True)
            return
        item = catalog.get_item(self.item["id"])
        await interaction.followup.send(
            f"Repriced: {price_line(item['name'], item['price_coins'], item['price_unit_pieces'], item['stack_size'])}",
            ephemeral=True,
        )


class _StockDeltaModal(discord.ui.Modal):
    def __init__(self, item: dict):
        super().__init__(title=f"Adjust stock: {item['name']}"[:45], timeout=300)
        self.item = item
        self.delta = discord.ui.TextInput(
            label="Change (+/- pieces)", placeholder="e.g. 128 or -64", max_length=8
        )
        self.add_item(self.delta)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            delta = int(str(self.delta.value).strip())
            new_qty = catalog.adjust_stock(self.item["id"], delta)
        except (catalog.CatalogError, ValueError) as err:
            await interaction.followup.send(f"Could not adjust stock: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"{self.item['name']} is now at {new_qty} pieces.", ephemeral=True
        )


class _ThresholdModal(discord.ui.Modal):
    def __init__(self, item: dict):
        super().__init__(title=f"Threshold: {item['name']}"[:45], timeout=300)
        self.item = item
        self.threshold = discord.ui.TextInput(
            label="Restock threshold (pieces)", placeholder="e.g. 100", max_length=8
        )
        self.add_item(self.threshold)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pieces = int(str(self.threshold.value).strip())
            alerts.set_threshold(self.item["id"], threshold_pieces=pieces)
        except (alerts.AlertError, ValueError) as err:
            await interaction.followup.send(f"Could not set threshold: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"{self.item['name']} will alert below {pieces} pieces.", ephemeral=True
        )


class _OpenMarketModal(discord.ui.Modal, title="Open a prediction market"):
    question = discord.ui.TextInput(label="Question", max_length=200)
    outcomes = discord.ui.TextInput(label="Outcomes (comma-separated)", placeholder="Yes, No")
    rake = discord.ui.TextInput(label="Rake, in basis points", default="0", max_length=5)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome_list = [o.strip() for o in str(self.outcomes.value).split(",") if o.strip()]
        try:
            market_id = predictions.open_market(
                str(self.question.value).strip(), outcome_list,
                created_by=money.user(interaction.user.id),
                rake_bps=int(str(self.rake.value).strip()),
            )
        except (predictions.MarketError, ValueError) as err:
            await interaction.followup.send(f"Could not open market: {err}", ephemeral=True)
            return
        code = addressing.mint("pred_market", market_id)
        await interaction.followup.send(f"Opened market, address {code}.", ephemeral=True)


class _ResolveConfirmModal(discord.ui.Modal):
    """Typed confirmation for an irreversible payout: the winning outcome's
    NAME, never the market's id."""

    def __init__(self, market: dict, outcome: str, resolver: str):
        super().__init__(title="Confirm resolution", timeout=300)
        self.market, self.outcome, self.resolver = market, outcome, resolver
        self.confirm = discord.ui.TextInput(
            label=f"Type the winning outcome: {outcome}"[:45],
            placeholder="Type it exactly as shown above", max_length=100,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.outcome.strip().lower():
            await interaction.followup.send("Outcome didn't match · resolution cancelled.",
                                             ephemeral=True)
            return
        event_id = money.new_event_id("pred.resolve")
        try:
            result = predictions.resolve(self.market["id"], self.outcome, event_id,
                                        actor=self.resolver)
        except (predictions.MarketError, money.MoneyError) as err:
            await interaction.followup.send(f"Could not resolve: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Resolved \"{self.market['question']}\" to \"{self.outcome}\". "
            f"Pool {money_text(result['pool'])}, paid out {money_text(result['paid_out'])}, "
            f"rake {money_text(result['rake'])}.",
            ephemeral=True,
        )


class _VoidConfirmModal(discord.ui.Modal):
    """Typed confirmation for an irreversible refund: the market's own
    question, its NAME -- never its id."""

    def __init__(self, market: dict, voider: str):
        super().__init__(title="Confirm void", timeout=300)
        self.market = market
        # Who is doing this. Without it the audit row for the most
        # destructive action in the bot reads "unknown".
        self.resolver = voider
        short = market["question"][:90]
        self.confirm = discord.ui.TextInput(
            label=f"Type the market question: {short}"[:45],
            placeholder="Type it exactly as shown above", max_length=200,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.market["question"].strip().lower():
            await interaction.followup.send("Question didn't match · void cancelled.", ephemeral=True)
            return
        try:
            released = predictions.void(self.market["id"], actor=self.resolver)
        except (predictions.MarketError, money.MoneyError) as err:
            await interaction.followup.send(f"Could not void: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Voided \"{self.market['question']}\" \u00b7 released {released} stake(s).", ephemeral=True
        )


class AdminPanelView(_StaffGatedView):
    @discord.ui.button(label="Add item", style=discord.ButtonStyle.primary)
    async def add_item(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_AddItemModal())

    @discord.ui.button(label="Reprice", style=discord.ButtonStyle.secondary)
    async def reprice(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, item: dict) -> None:
            await inter.response.send_modal(_RepriceModal(item))

        await interaction.response.send_message(
            "Pick an item to reprice:",
            view=pickers.ItemPickerView(self.owner_id, picked, active_only=False),
            ephemeral=True,
        )

    @discord.ui.button(label="Adjust stock", style=discord.ButtonStyle.secondary)
    async def stock(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, item: dict) -> None:
            await inter.response.send_modal(_StockDeltaModal(item))

        await interaction.response.send_message(
            "Pick an item to adjust stock for:",
            view=pickers.ItemPickerView(self.owner_id, picked, active_only=False),
            ephemeral=True,
        )

    @discord.ui.button(label="Set threshold", style=discord.ButtonStyle.secondary)
    async def threshold(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, item: dict) -> None:
            await inter.response.send_modal(_ThresholdModal(item))

        await interaction.response.send_message(
            "Pick an item to set a restock threshold for:",
            view=pickers.ItemPickerView(self.owner_id, picked, active_only=False),
            ephemeral=True,
        )

    @discord.ui.button(label="Open market", style=discord.ButtonStyle.primary, row=1)
    async def open_market(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_OpenMarketModal())

    @discord.ui.button(label="Resolve market", style=discord.ButtonStyle.secondary, row=1)
    async def resolve_market(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        markets = queries.list_open_markets(include_closed=True, limit=25)
        options = [(m["question"][:100], str(m["id"])) for m in markets]

        async def market_picked(inter: discord.Interaction, market_id_str: str) -> None:
            if market_id_str == "_none":
                await inter.response.send_message("No markets to resolve.", ephemeral=True)
                return
            market = queries.get_market_detail(int(market_id_str))
            # Keyed on the outcome's id, never its label text -- see
            # bot/views/pickers.py's note on why a long label makes an
            # unkeyed picker unbuildable.
            by_id = {str(o["id"]): o["label"] for o in market["outcomes"]}
            outcome_options = [(o["label"][:100], str(o["id"])) for o in market["outcomes"]]

            async def outcome_picked(inter2: discord.Interaction, outcome_id_str: str) -> None:
                outcome = by_id.get(outcome_id_str)
                if outcome is None:
                    await inter2.response.send_message("That outcome no longer exists.", ephemeral=True)
                    return
                await inter2.response.send_message(
                    f"Resolving \"{market['question']}\" to \"{outcome}\" will pay every winning "
                    f"stake pro-rata out of a pool of {money_text(market['pool'])}. This cannot be undone.",
                    view=_ResolveGate(market, outcome, money.user(inter2.user.id)),
                    ephemeral=True,
                )

            await inter.response.send_message(
                f"Pick the winning outcome for \"{market['question']}\":",
                view=pickers.OptionPickerView(self.owner_id, outcome_options, outcome_picked),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a market to resolve:",
            view=pickers.OptionPickerView(self.owner_id, options, market_picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Void market", style=discord.ButtonStyle.secondary, row=1)
    async def void_market(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        markets = queries.list_open_markets(include_closed=True, limit=25)
        options = [(m["question"][:100], str(m["id"])) for m in markets]

        async def picked(inter: discord.Interaction, market_id_str: str) -> None:
            if market_id_str == "_none":
                await inter.response.send_message("No markets to void.", ephemeral=True)
                return
            market = queries.get_market_detail(int(market_id_str))
            await inter.response.send_message(
                f"Voiding \"{market['question']}\" refunds every stake in full. This cannot be undone.",
                view=_VoidGate(market, money.user(inter.user.id)),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a market to void:",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Treasury", style=discord.ButtonStyle.secondary, row=2)
    async def treasury(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=build_treasury_embed(), ephemeral=True)

    @discord.ui.button(label="Fund treasury", style=discord.ButtonStyle.primary, row=2)
    async def fund(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Owner-only. Without this nothing can put gold into the system and
        every payout fails with an empty treasury."""
        config = getattr(interaction.client, "nola_config", None)
        if config is None or not permissions.is_owner(interaction.user, config):
            await interaction.response.send_message(
                "Owners only.", ephemeral=True)
            return
        funder = money.user(interaction.user.id)
        options = [(label, subject) for subject, label in money.TREASURY_NAMES.items()]

        async def picked(inter: discord.Interaction, subject: str) -> None:
            await inter.response.send_modal(
                _FundAmountModal(subject, funder, config))

        await interaction.response.send_message(
            "Which treasury?",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )


class _ResolveGate(discord.ui.View):
    def __init__(self, market: dict, outcome: str, resolver: str) -> None:
        super().__init__(timeout=120)
        self.market, self.outcome, self.resolver = market, outcome, resolver

    @discord.ui.button(label="Confirm resolution", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            _ResolveConfirmModal(self.market, self.outcome, self.resolver)
        )


class _VoidGate(discord.ui.View):
    def __init__(self, market: dict, voider: str) -> None:
        super().__init__(timeout=120)
        self.market = market
        self.voider = voider

    @discord.ui.button(label="Confirm void", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_VoidConfirmModal(self.market, self.voider))
