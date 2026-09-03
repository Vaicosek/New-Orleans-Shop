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

from core import alerts, audit, auctions as auctions_core, bonds as bonds_core, catalog, db, land as land_core, loans as loans_core, loyalty, money, orders as orders_core, predictions, pricing

from .. import addressing, loyalty_sync

from . import auctions as auction_views
from . import land as land_views
from . import bonds as bond_views
from . import pickers
from .. import layout
from .. import permissions
from ..permissions import is_staff
from ..ui.embed import money_text, panel_embed, price_line, rows
from .. import queries


# Cap on an outcome label's length, set when a market is OPENED. It exists
# so _ResolveConfirmModal's typed-confirmation TextInput (max_length=100)
# can never be asked to hold a longer label than Discord will let the user
# submit -- see _OpenMarketModal.on_submit and _ResolveConfirmModal below.
OUTCOME_LABEL_MAX = 100

# How much of a (possibly up-to-100-char) outcome label staff are actually
# asked to retype at resolution. The full label is still shown for
# legibility; the token is a truncated prefix of it, not an id, so what's
# typed stays human-legible per the "typed confirmation is a NAME never an
# id" rule -- it just isn't a typing ordeal for a label near the cap.
CONFIRM_TOKEN_MAX = 40


def _confirm_token(label: str, limit: int = CONFIRM_TOKEN_MAX) -> str:
    label = label.strip()
    return label if len(label) <= limit else label[:limit].rstrip()


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
        except ValueError as err:
            await interaction.followup.send(f"Could not adjust stock: {err}", ephemeral=True)
            return
        try:
            new_qty = catalog.adjust_stock(self.item["id"], delta)
        except catalog.CatalogError:
            # catalog's own error names the item by its raw database id --
            # correct for its own callers, but staff here only ever picked a
            # NAME from the item picker and never saw an id. Re-report by
            # name, with the figures that explain the refusal (current
            # pieces on hand and what this change would have made it),
            # rather than surfacing the id-bearing message verbatim.
            name = self.item["name"]
            try:
                current = catalog.get_stock(self.item["id"])["pieces"]
                attempted = current + delta
                await interaction.followup.send(
                    f"Could not adjust stock for {name}: currently {current} piece(s), "
                    f"change of {delta:+d} would make {attempted} — out of bounds.",
                    ephemeral=True,
                )
            except catalog.CatalogError:
                await interaction.followup.send(
                    f"Could not adjust stock for {name}: change of {delta:+d} was refused.",
                    ephemeral=True,
                )
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
        # An outcome label this long can never be typed back at resolution:
        # _ResolveConfirmModal's confirmation TextInput is capped at
        # OUTCOME_LABEL_MAX chars, and Discord refuses client-side to submit
        # a longer field, which would leave the market permanently stuck
        # open. Reject rather than silently truncate a label the player
        # will see and bet against.
        too_long = [o for o in outcome_list if len(o) > OUTCOME_LABEL_MAX]
        if too_long:
            bad = "; ".join(f"{o[:40]}... ({len(o)} chars)" for o in too_long)
            await interaction.followup.send(
                f"Outcome label(s) too long (max {OUTCOME_LABEL_MAX} chars): {bad}",
                ephemeral=True,
            )
            return
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
    """Typed confirmation for an irreversible payout: a short, human-legible
    token drawn from the winning outcome's NAME (never the market's or
    outcome's id, and never the full label verbatim if it's a long one --
    see `_confirm_token`)."""

    def __init__(self, market: dict, outcome: str, resolver: str, event_id: str):
        super().__init__(title="Confirm resolution", timeout=300)
        self.market, self.outcome, self.resolver = market, outcome, resolver
        # Minted ONCE, at the outcome-picked preview (see
        # AdminPanelView.resolve_market's outcome_picked), and carried
        # unchanged through _ResolveGate into here, so a Discord retry or a
        # resubmit of this same modal reuses the identical event id and
        # lands on predictions.resolve's own idempotent replay instead of
        # minting a second, different id that resolve() would then refuse
        # as AlreadyResolved.
        self.event_id = event_id
        self.token = _confirm_token(outcome)
        self.confirm = discord.ui.TextInput(
            label=f"Type the winning outcome: {self.token}"[:45],
            placeholder="Type it exactly as shown above", max_length=CONFIRM_TOKEN_MAX,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.token.strip().lower():
            await interaction.followup.send("Outcome didn't match · resolution cancelled.",
                                             ephemeral=True)
            return
        try:
            result = predictions.resolve(self.market["id"], self.outcome, self.event_id,
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


class _ListLandDetailsModal(discord.ui.Modal):
    """Step 1 of 2: the plot itself, all free text -- there is no catalog
    entry for land to pick from, unlike an item auction's lot. Split from
    pricing into its own modal because Discord caps a modal at 5 fields and
    name/description/location/min bid/min raise/buy-now/duration is 7."""

    def __init__(self, config):
        super().__init__(title="List land (1/2): details", timeout=300)
        self.config = config
        self.name = discord.ui.TextInput(label="Plot name", placeholder="e.g. Riverside Lot 4",
                                          max_length=100)
        self.description = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False,
            placeholder="Size, features, anything a buyer should know", max_length=500)
        self.location = discord.ui.TextInput(
            label="Location", required=False, placeholder="e.g. spawn +200/-450", max_length=200)
        for field in (self.name, self.description, self.location):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        details = {
            "name": str(self.name.value).strip(),
            "description": str(self.description.value).strip(),
            "location": str(self.location.value).strip(),
        }
        await interaction.response.send_modal(_ListLandPricingModal(details, self.config))


class _ListLandPricingModal(discord.ui.Modal):
    """Step 2 of 2: everything that is a quantity. Buy-now is optional --
    left blank, the plot is bid-only, exactly like an item auction."""

    def __init__(self, details: dict, config):
        super().__init__(title=f"List land (2/2): {details['name']}"[:45], timeout=300)
        self.details = details
        self.config = config
        self.min_bid = discord.ui.TextInput(label="Minimum bid (g)", placeholder="e.g. 500",
                                             max_length=10)
        self.min_increment = discord.ui.TextInput(
            label="Minimum raise (g)", placeholder="e.g. 50", max_length=10)
        self.buy_now = discord.ui.TextInput(
            label="Buy-now price (g), optional", required=False,
            placeholder="Leave blank for bid-only", max_length=10)
        self.duration = discord.ui.TextInput(
            label="Duration, in minutes", placeholder="e.g. 1440 (24h)", max_length=6)
        # A modal holds five fields at most; this is the fifth. Blank means
        # an outright sale, the original land model. A figure makes it a
        # STALL: the winning bid is the deposit, and the winner owes this
        # much every 30 days or loses the plot -- recurring income from
        # space in the shop's own district, which is scarce on any server.
        self.rent = discord.ui.TextInput(
            label="Rent per 30 days (g), optional", required=False,
            placeholder="Blank = sold outright. A figure = a stall", max_length=10)
        for field in (self.min_bid, self.min_increment, self.buy_now, self.duration, self.rent):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            min_bid = int(str(self.min_bid.value).strip())
            min_increment = int(str(self.min_increment.value).strip())
            duration_minutes = int(str(self.duration.value).strip())
            buy_now_raw = str(self.buy_now.value).strip()
            buy_now_price = int(buy_now_raw) if buy_now_raw else None
            rent_raw = str(self.rent.value).strip()
            rent_coins = int(rent_raw) if rent_raw else 0
        except ValueError:
            await interaction.followup.send(
                "Minimum bid, minimum raise, duration and buy-now (if given) must all be "
                "whole numbers.", ephemeral=True)
            return
        try:
            land_id = land_core.open_listing(
                self.details["name"], self.details["description"], self.details["location"],
                min_bid, min_increment, duration_minutes,
                buy_now_price=buy_now_price, rent_coins=rent_coins,
                created_by=money.user(interaction.user.id),
            )
        except land_core.LandError as err:
            await interaction.followup.send(f"Could not list that plot: {err}", ephemeral=True)
            return

        # Post the card BEFORE telling staff it's done -- same reasoning as
        # _OpenAuctionModal: the listing already exists and is settleable
        # even if the public card never posts.
        channel = layout.channel(interaction.client, self.config, "channel:land")
        post_note = ""
        if channel is None:
            post_note = (" Could not find the land channel to post a public card -- "
                         "the listing still exists and will still settle on time.")
        else:
            try:
                embed = land_views.build_land_embed(land_id)
                posted = await channel.send(embed=embed, view=land_views.LandCardView())
            except discord.HTTPException as err:
                post_note = f" Could not post the public card ({err})."
            else:
                if posted is not None:
                    land_core.set_message(land_id, str(channel.id), str(posted.id))
                    post_note = " Posted in the land channel."

        buy_now_note = f", buy now {money_text(buy_now_price)}" if buy_now_price else ""
        buy_now_note += f", then {money_text(rent_coins)} rent every 30 days" if rent_coins else ""
        await interaction.followup.send(
            f"Listed \"{self.details['name']}\" (#{land_id}): minimum bid "
            f"{money_text(min_bid)}{buy_now_note}, closes in {duration_minutes} "
            f"minute(s).{post_note}",
            ephemeral=True,
        )


class _IssueBondDetailsModal(discord.ui.Modal):
    """Step 1 of 2: name, unit price, and how many units exist. Split from
    the rate/term fields into its own modal for the same reason as land's
    two-step listing modal -- Discord caps a modal at 5 fields and this is
    6 total."""

    def __init__(self, config):
        super().__init__(title="Issue bond (1/2): units", timeout=300)
        self.config = config
        self.name = discord.ui.TextInput(label="Bond name", placeholder="e.g. Shop Bond Series 1",
                                          max_length=100)
        self.unit_price = discord.ui.TextInput(label="Price per unit (g)", placeholder="e.g. 100",
                                                 max_length=10)
        self.units_total = discord.ui.TextInput(label="Total units", placeholder="e.g. 500",
                                                  max_length=8)
        for field in (self.name, self.unit_price, self.units_total):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            unit_price = int(str(self.unit_price.value).strip())
            units_total = int(str(self.units_total.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Price per unit and total units must be whole numbers.", ephemeral=True)
            return
        details = {"name": str(self.name.value).strip(), "unit_price": unit_price,
                   "units_total": units_total}
        await interaction.response.send_modal(_IssueBondTermsModal(details, self.config))


class _IssueBondTermsModal(discord.ui.Modal):
    """Step 2 of 2: the rate and the calendar. Coupon rate is typed in
    basis points (100 = 1%) -- an integer unit that can express "0.5% a
    month" (50 bps) without a decimal string, same reasoning as
    core/bonds.py's `coupon_bps` column."""

    def __init__(self, details: dict, config):
        super().__init__(title=f"Issue bond (2/2): {details['name']}"[:45], timeout=300)
        self.details = details
        self.config = config
        self.coupon_bps = discord.ui.TextInput(
            label="Coupon rate, bps (100 = 1%)", placeholder="e.g. 200 for 2%", max_length=6)
        self.coupon_interval_days = discord.ui.TextInput(
            label="Coupon every N days", placeholder="e.g. 30", max_length=5)
        self.term_days = discord.ui.TextInput(
            label="Term, in days", placeholder="e.g. 90", max_length=5)
        for field in (self.coupon_bps, self.coupon_interval_days, self.term_days):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            coupon_bps = int(str(self.coupon_bps.value).strip())
            coupon_interval_days = int(str(self.coupon_interval_days.value).strip())
            term_days = int(str(self.term_days.value).strip())
        except ValueError:
            await interaction.followup.send(
                "Coupon rate, coupon interval and term must all be whole numbers.",
                ephemeral=True)
            return
        try:
            bond_id = bonds_core.issue(
                self.details["name"], self.details["unit_price"], self.details["units_total"],
                coupon_bps, coupon_interval_days, term_days,
                created_by=money.user(interaction.user.id),
            )
        except bonds_core.BondError as err:
            await interaction.followup.send(f"Could not issue that bond: {err}", ephemeral=True)
            return

        channel = layout.channel(interaction.client, self.config, "channel:bonds")
        post_note = ""
        if channel is None:
            post_note = (" Could not find the bonds channel to post a public card -- "
                         "the bond still exists and will still pay coupons on time.")
        else:
            try:
                embed = bond_views.build_bond_embed(bond_id)
                posted = await channel.send(embed=embed, view=bond_views.BondCardView())
            except discord.HTTPException as err:
                post_note = f" Could not post the public card ({err})."
            else:
                if posted is not None:
                    bonds_core.set_message(bond_id, str(channel.id), str(posted.id))
                    post_note = " Posted in the bonds channel."

        await interaction.followup.send(
            f"Issued bond \"{self.details['name']}\" (#{bond_id}): "
            f"{self.details['units_total']:,} unit(s) at "
            f"{money_text(self.details['unit_price'])} each, coupon every "
            f"{coupon_interval_days} day(s), term {term_days} day(s).{post_note}",
            ephemeral=True,
        )


class _OpenAuctionModal(discord.ui.Modal):
    """Duration, pieces, minimum bid and minimum raise are all free-text
    quantities -- the lot itself is already resolved by the item picker
    that opens this modal, never typed."""

    def __init__(self, item: dict, config):
        super().__init__(title=f"Auction: {item['name']}"[:45], timeout=300)
        self.item = item
        self.config = config
        self.pieces = discord.ui.TextInput(label="Pieces", placeholder="e.g. 64", max_length=8)
        self.min_bid = discord.ui.TextInput(label="Minimum bid (g)", placeholder="e.g. 500", max_length=10)
        self.min_increment = discord.ui.TextInput(
            label="Minimum raise (g)", placeholder="e.g. 50", max_length=10)
        self.duration = discord.ui.TextInput(
            label="Duration, in minutes", placeholder="e.g. 1440 (24h)", max_length=6)
        for field in (self.pieces, self.min_bid, self.min_increment, self.duration):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pieces = int(str(self.pieces.value).strip())
            min_bid = int(str(self.min_bid.value).strip())
            min_increment = int(str(self.min_increment.value).strip())
            duration_minutes = int(str(self.duration.value).strip())
        except ValueError:
            await interaction.followup.send("Every field must be a whole number.", ephemeral=True)
            return
        try:
            auction_id = auctions_core.open_auction(
                self.item["id"], pieces, min_bid, min_increment, duration_minutes,
                created_by=money.user(interaction.user.id),
            )
        except auctions_core.AuctionError as err:
            await interaction.followup.send(f"Could not open that auction: {err}", ephemeral=True)
            return

        # Post the card BEFORE telling staff it's done, same reasoning as
        # bot/views/shop.py's order card: the auction itself already exists
        # and is settleable even if the public card never posts -- only the
        # card is at risk here.
        channel = layout.channel(interaction.client, self.config, "channel:auctions")
        post_note = ""
        if channel is None:
            post_note = (" Could not find the auctions channel to post a public card -- "
                         "the auction still exists and will still settle on time.")
        else:
            try:
                embed = auction_views.build_auction_embed(auction_id)
                posted = await channel.send(embed=embed, view=auction_views.AuctionCardView())
            except discord.HTTPException as err:
                post_note = f" Could not post the public card ({err})."
            else:
                if posted is not None:
                    auctions_core.set_message(auction_id, str(channel.id), str(posted.id))
                    post_note = " Posted in the auctions channel."

        await interaction.followup.send(
            f"Opened auction #{auction_id}: {pieces} × {self.item['name']}, "
            f"minimum bid {money_text(min_bid)}, closes in {duration_minutes} "
            f"minute(s).{post_note}",
            ephemeral=True,
        )


class _VoidAuctionConfirmModal(discord.ui.Modal):
    """Typed confirmation for an irreversible refund: the auctioned item's
    own NAME -- never the auction's id."""

    def __init__(self, auction: dict, voider: str):
        super().__init__(title="Confirm void", timeout=300)
        self.auction = auction
        self.voider = voider
        self.confirm = discord.ui.TextInput(
            label=f"Type the item name: {auction['item_name']}"[:45],
            placeholder="Type it exactly as shown above", max_length=100,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.auction["item_name"].strip().lower():
            await interaction.followup.send("Item name didn't match · void cancelled.", ephemeral=True)
            return
        try:
            auctions_core.void(self.auction["id"], actor=self.voider)
        except (auctions_core.AuctionError, money.MoneyError) as err:
            await interaction.followup.send(f"Could not void: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Voided auction #{self.auction['id']} ({self.auction['item_name']}).", ephemeral=True)


class _VoidLandConfirmModal(discord.ui.Modal):
    """Typed confirmation for an irreversible refund: the plot's own NAME
    -- never the listing's id. Same shape as _VoidAuctionConfirmModal."""

    def __init__(self, listing: dict, voider: str):
        super().__init__(title="Confirm void", timeout=300)
        self.listing = listing
        self.voider = voider
        self.confirm = discord.ui.TextInput(
            label=f"Type the plot name: {listing['name']}"[:45],
            placeholder="Type it exactly as shown above", max_length=100,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.listing["name"].strip().lower():
            await interaction.followup.send("Plot name didn't match \u00b7 void cancelled.",
                                             ephemeral=True)
            return
        try:
            land_core.void(self.listing["id"], actor=self.voider)
        except (land_core.LandError, money.MoneyError) as err:
            await interaction.followup.send(f"Could not void: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Voided land listing #{self.listing['id']} ({self.listing['name']}).", ephemeral=True)


class _VoidLandGate(discord.ui.View):
    def __init__(self, listing: dict, voider: str) -> None:
        super().__init__(timeout=120)
        self.listing = listing
        self.voider = voider

    @discord.ui.button(label="Confirm void", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_VoidLandConfirmModal(self.listing, self.voider))


class _VoidBondConfirmModal(discord.ui.Modal):
    """Typed confirmation for an irreversible refund: the bond's own NAME
    -- never its id. Same shape as _VoidAuctionConfirmModal/_VoidLandConfirmModal."""

    def __init__(self, bond: dict, voider: str):
        super().__init__(title="Confirm void", timeout=300)
        self.bond = bond
        self.voider = voider
        self.confirm = discord.ui.TextInput(
            label=f"Type the bond name: {bond['name']}"[:45],
            placeholder="Type it exactly as shown above", max_length=100,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().lower() != self.bond["name"].strip().lower():
            await interaction.followup.send("Bond name didn't match \u00b7 void cancelled.",
                                             ephemeral=True)
            return
        try:
            bonds_core.void(self.bond["id"], actor=self.voider)
        except (bonds_core.BondError, money.MoneyError) as err:
            await interaction.followup.send(f"Could not void: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Voided bond #{self.bond['id']} ({self.bond['name']}).", ephemeral=True)


class _VoidBondGate(discord.ui.View):
    def __init__(self, bond: dict, voider: str) -> None:
        super().__init__(timeout=120)
        self.bond = bond
        self.voider = voider

    @discord.ui.button(label="Confirm void", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_VoidBondConfirmModal(self.bond, self.voider))


class _WriteOffLoanGate(discord.ui.View):
    """No typed name: unlike void, nothing here moves money -- the treasury
    already lost the principal at disbursement, this only stops chasing
    it. A single Confirm button on an already-previewed figure is enough,
    same as _WriteOffLoanGate's own preview text shows the real amount
    before this ever renders."""

    def __init__(self, loan: dict, actor: str) -> None:
        super().__init__(timeout=120)
        self.loan = loan
        self.actor = actor

    @discord.ui.button(label="Confirm write-off", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            loans_core.write_off(self.loan["id"], actor=self.actor)
        except loans_core.LoanError as err:
            await interaction.followup.send(f"Could not write that off: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Wrote off loan #{self.loan['id']} ({self.loan['subject']}).", ephemeral=True)


class _MarginModal(discord.ui.Modal):
    """What share of an item's sell price a worker is paid.

    The shop's margin is the remainder, and it is a business number rather
    than an engineering one -- on a host with no shell, changing it in code
    means a push and a panel pull, so it lives in `config` and is set here.

    Existing orders are NOT affected: every order snapshots its own rate at
    creation, so a change here decides what NEW orders pay and can never
    reprice work somebody already claimed.
    """

    def __init__(self) -> None:
        current = orders_core.payout_pct()
        super().__init__(title="Worker share of the sell price", timeout=300)
        self.pct = discord.ui.TextInput(
            label="Worker share, in percent",
            placeholder=f"currently {current} -- the shop keeps {100 - current}%",
            max_length=3, required=True,
        )
        self.add_item(self.pct)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            value = int(str(self.pct.value).strip())
        except ValueError:
            await interaction.followup.send(
                "That needs to be a whole number of percent.", ephemeral=True)
            return
        if not 1 <= value <= 100:
            await interaction.followup.send(
                "The worker share has to be between 1 and 100 percent. A share of 0 "
                "pays nothing and leaves delivered work unpayable.", ephemeral=True)
            return
        # A top-rank bonus and a manager override both land ON TOP of the
        # worker's figure, so a share this high costs more than the sale
        # that funds it. Say so with the real arithmetic rather than
        # refusing a number that is legal.
        warning = ""
        if value > 85:
            loaded = value * 112 // 100
            loaded += loaded * 5 // 100
            if loaded >= 100:
                warning = (f" Careful: at {value}%, an order worked by a top-rank member "
                            f"of a team costs about {loaded}% of the sale once their bonus "
                            f"and the manager's cut land on top -- the shop loses money on it.")
        db.set_config(orders_core.PAYOUT_PCT_KEY, str(value))
        await interaction.followup.send(
            f"Workers are now paid {value}% of the sell price; the shop keeps "
            f"{100 - value}%. Orders already open keep the rate they were opened "
            f"at.{warning}", ephemeral=True)


class _CategoryModal(discord.ui.Modal):
    """Name, sort order, note. All three are genuinely free text -- a
    category has no id to pick from, and creating one is the point -- so
    this is a modal rather than a picker, unlike every flow that keys on an
    item or an order. `upsert_category` is an upsert, so typing an existing
    name renumbers or re-notes it rather than failing."""

    def __init__(self) -> None:
        super().__init__(title="Category", timeout=300)
        self.name = discord.ui.TextInput(
            label="Category name", placeholder="e.g. Brewing", max_length=40, required=True)
        self.sort_order = discord.ui.TextInput(
            label="Sort order (lower shows first)", placeholder="e.g. 40",
            max_length=5, required=True)
        self.note = discord.ui.TextInput(
            label="Note (optional)", placeholder="What belongs in it",
            max_length=200, required=False)
        for field in (self.name, self.sort_order, self.note):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        name = str(self.name.value).strip()
        if not name:
            await interaction.followup.send("A category needs a name.", ephemeral=True)
            return
        try:
            sort_order = int(str(self.sort_order.value).strip())
        except ValueError:
            await interaction.followup.send(
                "Sort order must be a whole number.", ephemeral=True)
            return
        existed = any(c["name"].lower() == name.lower()
                      for c in catalog.list_categories())
        try:
            catalog.upsert_category(name, sort_order,
                                     note=str(self.note.value).strip() or None)
        except Exception as err:  # noqa: BLE001 -- refuse, don't half-apply
            await interaction.followup.send(f"Could not save that: {err}", ephemeral=True)
            return
        await interaction.followup.send(
            f"{'Updated' if existed else 'Created'} category **{name}** "
            f"at sort order {sort_order}.", ephemeral=True)


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

    @discord.ui.button(label="Retire / restore", style=discord.ButtonStyle.secondary)
    async def retire(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        # Reversible toggle, not irreversible like resolve/void -- so this
        # follows Adjust stock's pattern (picker, then the staff gate
        # already re-checked on every click by _StaffGatedView.interaction_check
        # is the whole confirmation), not a typed-confirmation modal.
        # active_only=False so an already-retired item is still reachable
        # here to be restored.
        async def picked(inter: discord.Interaction, item: dict) -> None:
            await inter.response.defer(ephemeral=True)
            if item["active"]:
                catalog.deactivate_item(item["id"])
                await inter.followup.send(f"{item['name']} retired.", ephemeral=True)
                return
            catalog.activate_item(item["id"])
            await inter.followup.send(f"{item['name']} restored.", ephemeral=True)

        await interaction.response.send_message(
            "Pick an item to retire or restore:",
            view=pickers.ItemPickerView(self.owner_id, picked, active_only=False),
            ephemeral=True,
        )

    @discord.ui.button(label="Margin", style=discord.ButtonStyle.secondary)
    async def margin(self, interaction: discord.Interaction,
                      _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_MarginModal())

    @discord.ui.button(label="Categories", style=discord.ButtonStyle.secondary)
    async def categories(self, interaction: discord.Interaction,
                          _button: discord.ui.Button) -> None:
        """Create a category, or renumber one that exists.

        `catalog.upsert_category` shipped with exactly one caller --
        `seed_catalog.py`, which needs a shell the host does not provide --
        so on the live server a category could only ever be one the seed
        happened to include. That is now load-bearing: a team declares the
        categories it works (CONTRACT.md 11d), and an order reaches the
        right crew by matching them, so "we sell a new kind of thing" had
        no way to become "this crew handles it".
        """
        await interaction.response.send_modal(_CategoryModal())

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
            market_id = int(market_id_str)
            # Close the moment staff commit to resolving THIS market -- before
            # the outcome picker, the preview, or the confirm modal are ever
            # shown. That gap is exactly the window an insider who already
            # knows the outcome could use to slip in a stake; closing here
            # means predictions.resolve() (which now requires a closed
            # market) never has to trust that nothing landed in between.
            try:
                predictions.close(market_id)
            except predictions.MarketNotOpen:
                pass  # already closed is fine; voided/resolved is caught below
            market = queries.get_market_detail(market_id)
            if market is None or market["status"] != "closed":
                await inter.response.send_message(
                    "That market is no longer open for resolution.", ephemeral=True)
                return
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
                # Minted here, at the FIRST preview of this resolution, and
                # carried unchanged through _ResolveGate and into
                # _ResolveConfirmModal -- never re-minted on modal submit.
                # That way a Discord-retried interaction or a staff resubmit
                # of the confirm modal reuses the same event id both times
                # and lands on predictions.resolve's own idempotent replay
                # rather than minting a second id that resolve() refuses as
                # AlreadyResolved even though the first resolve succeeded.
                event_id = money.new_event_id("pred.resolve")
                await inter2.response.send_message(
                    f"Resolving \"{market['question']}\" to \"{outcome}\" will pay every winning "
                    f"stake pro-rata out of a pool of {money_text(market['pool'])}. This cannot be undone.",
                    view=_ResolveGate(market, outcome, money.user(inter2.user.id), event_id),
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

    @discord.ui.button(label="Open auction", style=discord.ButtonStyle.primary, row=1)
    async def open_auction(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def picked(inter: discord.Interaction, item: dict) -> None:
            await inter.response.send_modal(_OpenAuctionModal(item, self.config))

        await interaction.response.send_message(
            "Pick an item to auction:",
            view=pickers.ItemPickerView(self.owner_id, picked, active_only=False),
            ephemeral=True,
        )

    @discord.ui.button(label="Void auction", style=discord.ButtonStyle.secondary, row=1)
    async def void_auction(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        auctions = queries.list_open_auctions(limit=25)
        options = [(f"{a['item_name']} (#{a['id']})"[:100], str(a["id"])) for a in auctions]

        async def picked(inter: discord.Interaction, auction_id_str: str) -> None:
            if auction_id_str == "_none":
                await inter.response.send_message("No auctions to void.", ephemeral=True)
                return
            auction = queries.get_auction_detail(int(auction_id_str))
            await inter.response.send_message(
                f"Voiding \"{auction['item_name']}\" refunds the current bid in full. "
                "This cannot be undone.",
                view=_VoidAuctionGate(auction, money.user(inter.user.id)),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick an auction to void:",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Treasury", style=discord.ButtonStyle.secondary, row=2)
    async def treasury(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=build_treasury_embed(), ephemeral=True)

    @discord.ui.button(label="Freeze / unfreeze wallet", style=discord.ButtonStyle.secondary, row=2)
    async def freeze_wallet(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        # Reversible toggle -- same pattern as Retire/restore: a picker,
        # then the staff gate already re-checked on every click by
        # _StaffGatedView.interaction_check IS the confirmation. A frozen
        # wallet refuses every hold and transfer outright (core.money's
        # place_hold/transfer), so this is the one button that can stop
        # someone's money moving at all.
        async def picked(inter: discord.Interaction, member: discord.abc.User) -> None:
            await inter.response.defer(ephemeral=True)
            subject = money.user(member.id)
            actor = money.user(interaction.user.id)
            # The money write and the audit row commit in ONE transaction --
            # CONTRACT.md sec 8 rule 6, "never a best-effort side call" --
            # money.freeze/unfreeze write no audit row themselves, so this
            # view is where that guarantee has to be made true.
            with db.db() as conn:
                bal = money.balance(subject, conn=conn)
                if bal.frozen:
                    money.unfreeze(subject, service="owner", actor=actor, conn=conn)
                    audit.record(conn, actor=actor, target=subject, kind="wallet.unfreeze",
                                 summary=f"unfroze {subject} via /admin",
                                 ops=[{"op": "unfreeze", "subject": subject, "reverse": "freeze"}])
                else:
                    money.freeze(subject, service="owner", actor=actor, conn=conn)
                    audit.record(conn, actor=actor, target=subject, kind="wallet.freeze",
                                 summary=f"froze {subject} via /admin",
                                 ops=[{"op": "freeze", "subject": subject, "reverse": "unfreeze"}])
            if bal.frozen:
                await inter.followup.send(f"{member.display_name}'s wallet unfrozen.", ephemeral=True)
            else:
                await inter.followup.send(f"{member.display_name}'s wallet frozen.", ephemeral=True)

        await interaction.response.send_message(
            "Pick a member to freeze or unfreeze:",
            view=pickers.UserPickerView(self.owner_id, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Block / allow gambling", style=discord.ButtonStyle.secondary, row=2)
    async def gambling_flag(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        # `games` has no FLAG scope (see core.money.SERVICE_SCOPES), so the
        # service this restriction governs can never lift it -- only this
        # owner-scoped staff action can.
        async def picked(inter: discord.Interaction, member: discord.abc.User) -> None:
            await inter.response.defer(ephemeral=True)
            subject = money.user(member.id)
            actor = money.user(interaction.user.id)
            with db.db() as conn:
                blocked = "gambling_blocked" in money.flags(subject, conn=conn)
                if blocked:
                    money.clear_flag(subject, "gambling_blocked", service="owner", conn=conn)
                    audit.record(conn, actor=actor, target=subject, kind="wallet.unblock_gambling",
                                 summary=f"allowed gambling for {subject} via /admin",
                                 ops=[{"op": "unblock_gambling", "subject": subject,
                                       "reverse": "block_gambling"}])
                else:
                    money.set_flag(subject, "gambling_blocked", service="owner", set_by=actor, conn=conn)
                    audit.record(conn, actor=actor, target=subject, kind="wallet.block_gambling",
                                 summary=f"blocked gambling for {subject} via /admin",
                                 ops=[{"op": "block_gambling", "subject": subject,
                                       "reverse": "unblock_gambling"}])
            if blocked:
                await inter.followup.send(f"{member.display_name} can gamble again.", ephemeral=True)
            else:
                await inter.followup.send(f"{member.display_name} is blocked from gambling.", ephemeral=True)

        await interaction.response.send_message(
            "Pick a member to block or allow gambling for:",
            view=pickers.UserPickerView(self.owner_id, picked),
            ephemeral=True,
        )

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

    @discord.ui.button(label="Set rank", style=discord.ButtonStyle.secondary, row=3)
    async def set_rank(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Owner-only, like Fund treasury -- a forced rank grants real
        benefits (order payout bonus, a raised MAX_BET/MAX_DAILY_LOSS), so
        this is the same class of privilege as minting coins, not an
        ordinary staff action."""
        config = getattr(interaction.client, "nola_config", None)
        if config is None or not permissions.is_owner(interaction.user, config):
            await interaction.response.send_message("Owners only.", ephemeral=True)
            return

        async def picked_member(inter: discord.Interaction, member: discord.abc.User) -> None:
            options = [(t["name"], t["key"]) for t in loyalty.TIERS]

            async def picked_rank(inter2: discord.Interaction, rank_key: str) -> None:
                await inter2.response.defer(ephemeral=True)
                subject = money.user(member.id)
                actor = money.user(interaction.user.id)
                with db.db() as conn:
                    loyalty.set_override(subject, rank_key, actor=actor, conn=conn)
                    audit.record(
                        conn, actor=actor, target=subject, kind="loyalty.set_rank",
                        summary=f"forced {subject} to rank {rank_key} via /admin",
                        ops=[{"op": "set_rank", "subject": subject, "rank_key": rank_key,
                              "reverse": "clear_rank"}],
                    )
                if inter2.guild_id is not None:
                    await loyalty_sync.sync_rank_role(inter2.client, inter2.guild_id, subject)
                await inter2.followup.send(
                    f"{member.display_name} is now forced to **{loyalty.TIERS_BY_KEY[rank_key]['name']}** "
                    "until cleared.", ephemeral=True)

            await inter.response.send_message(
                f"Which rank for {member.display_name}?",
                view=pickers.OptionPickerView(self.owner_id, options, picked_rank),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a member to set a forced rank for:",
            view=pickers.UserPickerView(self.owner_id, picked_member),
            ephemeral=True,
        )

    @discord.ui.button(label="Clear rank override", style=discord.ButtonStyle.secondary, row=3)
    async def clear_rank(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Owner-only, same gate as Set rank -- reverts a subject to their
        computed rank."""
        config = getattr(interaction.client, "nola_config", None)
        if config is None or not permissions.is_owner(interaction.user, config):
            await interaction.response.send_message("Owners only.", ephemeral=True)
            return

        async def picked(inter: discord.Interaction, member: discord.abc.User) -> None:
            await inter.response.defer(ephemeral=True)
            subject = money.user(member.id)
            actor = money.user(interaction.user.id)
            with db.db() as conn:
                cleared = loyalty.clear_override(subject, conn=conn)
                if cleared:
                    audit.record(
                        conn, actor=actor, target=subject, kind="loyalty.clear_rank",
                        summary=f"cleared {subject}'s forced rank via /admin",
                        ops=[{"op": "clear_rank", "subject": subject, "reverse": None}],
                    )
            if cleared and inter.guild_id is not None:
                await loyalty_sync.sync_rank_role(inter.client, inter.guild_id, subject)
            await inter.followup.send(
                f"{member.display_name} reverted to their computed rank."
                if cleared else f"{member.display_name} had no forced rank.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a member to clear a forced rank for:",
            view=pickers.UserPickerView(self.owner_id, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="List land", style=discord.ButtonStyle.primary, row=3)
    async def list_land(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_ListLandDetailsModal(self.config))

    @discord.ui.button(label="Void land", style=discord.ButtonStyle.secondary, row=3)
    async def void_land(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        listings = queries.list_open_land(limit=25)
        options = [(f"{l['name']} (#{l['id']})"[:100], str(l["id"])) for l in listings]

        async def picked(inter: discord.Interaction, land_id_str: str) -> None:
            if land_id_str == "_none":
                await inter.response.send_message("No listings to void.", ephemeral=True)
                return
            listing = queries.get_land_detail(int(land_id_str))
            await inter.response.send_message(
                f"Voiding \"{listing['name']}\" refunds the current bid in full. "
                "This cannot be undone.",
                view=_VoidLandGate(listing, money.user(inter.user.id)),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a listing to void:",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Issue bond", style=discord.ButtonStyle.primary, row=3)
    async def issue_bond(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_IssueBondModal(self.config))

    @discord.ui.button(label="Void bond", style=discord.ButtonStyle.secondary, row=4)
    async def void_bond(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        open_bonds = queries.list_open_bonds(limit=25)
        options = [(f"{b['name']} (#{b['id']})"[:100], str(b["id"])) for b in open_bonds]

        async def picked(inter: discord.Interaction, bond_id_str: str) -> None:
            if bond_id_str == "_none":
                await inter.response.send_message("No bonds to void.", ephemeral=True)
                return
            bond = queries.get_bond_detail(int(bond_id_str))
            await inter.response.send_message(
                f"Voiding \"{bond['name']}\" refunds every holder's principal in full "
                "(not coupons already paid). This cannot be undone.",
                view=_VoidBondGate(bond, money.user(inter.user.id)),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a bond to void:",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )

    @discord.ui.button(label="Write off loan", style=discord.ButtonStyle.secondary, row=4)
    async def write_off_loan(self, interaction: discord.Interaction,
                              _button: discord.ui.Button) -> None:
        open_loans = queries.list_open_loans(limit=25)
        options = [
            (f"{l['subject']} #{l['id']}: {l['principal'] + l['interest'] - l['paid']:,}g owed"[:100],
             str(l["id"]))
            for l in open_loans
        ]

        async def picked(inter: discord.Interaction, loan_id_str: str) -> None:
            if loan_id_str == "_none":
                await inter.response.send_message("No open loans.", ephemeral=True)
                return
            loan = queries.get_loan_detail(int(loan_id_str))
            remaining = loan["principal"] + loan["interest"] - loan["paid"]
            await inter.response.send_message(
                f"Write off loan #{loan['id']} ({loan['subject']})? This forgives "
                f"{money_text(remaining)} still owed -- the treasury already paid this out "
                "and will not get it back. This also frees their credit limit again.",
                view=_WriteOffLoanGate(loan, money.user(inter.user.id)),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a loan to write off:",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )


class _ResolveGate(discord.ui.View):
    def __init__(self, market: dict, outcome: str, resolver: str, event_id: str) -> None:
        super().__init__(timeout=120)
        self.market, self.outcome, self.resolver = market, outcome, resolver
        self.event_id = event_id

    @discord.ui.button(label="Confirm resolution", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            _ResolveConfirmModal(self.market, self.outcome, self.resolver, self.event_id)
        )


class _VoidGate(discord.ui.View):
    def __init__(self, market: dict, voider: str) -> None:
        super().__init__(timeout=120)
        self.market = market
        self.voider = voider

    @discord.ui.button(label="Confirm void", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_VoidConfirmModal(self.market, self.voider))


class _VoidAuctionGate(discord.ui.View):
    def __init__(self, auction: dict, voider: str) -> None:
        super().__init__(timeout=120)
        self.auction = auction
        self.voider = voider

    @discord.ui.button(label="Confirm void", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_VoidAuctionConfirmModal(self.auction, self.voider))
