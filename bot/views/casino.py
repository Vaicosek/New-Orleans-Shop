"""Casino panel: game picker, bet, round history, fairness verify.

Coinflip and dice only, per CONTRACT.md section 9. Every round result carries
a persistent "Verify" button -- registered once at boot with no round id
attached, re-resolving the round from the message it is on -- so a bet made
before a restart can still be checked afterwards.
"""
from __future__ import annotations

import re

import discord

from core import games, money

from . import pickers
from .. import addressing, queries
from ..ui.embed import SEP, money_text, panel_embed, rows

_ROUND_MARK = re.compile(r"round:(\S+)")

GAME_LABELS = {"coinflip": "Coinflip", "dice": "Dice"}
SELECTION_LABELS = {
    "coinflip": [("Heads", "heads"), ("Tails", "tails")],
    "dice": [(f"Roll {n}", str(n)) for n in range(1, 7)],
}


def _round_footer(round_id: str, code: str) -> str:
    return f"address {code}  {SEP} round:{round_id}"


def parse_round_id(message: discord.Message | None) -> str | None:
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer
    text = getattr(footer, "text", None)
    if not text:
        return None
    m = _ROUND_MARK.search(text)
    return m.group(1) if m else None


def build_result_embed(result: dict) -> discord.Embed:
    bet = result["results"][0] if result["results"] else None
    game = result["game"]
    outcome = result["outcome"]
    outcome_text = outcome.get("face") if game == "coinflip" else f"rolled {outcome.get('roll')}"
    if bet is None:
        body = f"Round settled: {outcome_text}."
    else:
        verdict = "WIN" if bet["win"] else "loss"
        body = (
            f"{GAME_LABELS.get(game, game)} {SEP} {outcome_text}\n"
            f"Selection: {bet['selection']}\n"
            f"Result: {verdict}\n"
            f"Payout: {money_text(bet['payout'])}"
        )
    code = addressing.mint("game_round", result["round_id"])
    return panel_embed(
        f"{GAME_LABELS.get(game, game)} round",
        body,
        footer=_round_footer(result["round_id"], code),
    )


def build_verify_embed(round_id: str) -> discord.Embed:
    try:
        v = games.verify(round_id)
    except games.GameError as err:
        return panel_embed("Verify round", f"Could not verify: {err}")
    body = (
        f"Commitment matches: {v['seed_matches_commitment']}\n"
        f"Outcome matches: {v['outcome_matches']}\n"
        f"Overall: {'VALID' if v['ok'] else 'INVALID'}\n\n"
        f"server_seed: {v['server_seed']}\n"
        f"server_seed_hash: {v['server_seed_hash']}\n"
        f"client_seed: {v['client_seed']}\n"
        f"nonce: {v['nonce']}"
    )
    return panel_embed(f"Verify {SEP} {v['game']} round", body)


class RoundVerifyView(discord.ui.View):
    """Persistent -- re-resolves the round id from the message it is on."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.secondary,
                        custom_id="nola:casino:verify")
    async def verify_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        round_id = parse_round_id(interaction.message)
        if round_id is None:
            await interaction.response.send_message("Could not identify this round.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=build_verify_embed(round_id), ephemeral=True)


class _BetModal(discord.ui.Modal):
    """The bet amount and the player's own seed are genuinely free text; game
    and selection are already resolved by the two pickers before this opens.

    The seed the player types IS the client seed, verbatim. It used to be
    `f"{subject}:{interaction.id}"` -- a value the house picked -- which made
    the "provably fair" round unprovable: with both seeds under the house's
    control there is nothing the player contributed to check against. The
    commitment this modal was opened under (`commitment_id`) was published to
    the player, hash and all, before the modal appeared.
    """

    def __init__(self, game: str, selection: str, subject: str,
                 commitment_id: str | None = None):
        super().__init__(title=f"Bet: {GAME_LABELS.get(game, game)}"[:45], timeout=300)
        self.game, self.selection, self.subject = game, selection, subject
        self.commitment_id = commitment_id
        self.amount = discord.ui.TextInput(
            label=f"Amount (g, max {games.MAX_BET:,})",
            placeholder="e.g. 50", max_length=10, required=True,
        )
        self.client_seed = discord.ui.TextInput(
            label="Your seed",
            placeholder="any words you like -- yours, not ours",
            max_length=64, required=True,
        )
        self.add_item(self.amount)
        self.add_item(self.client_seed)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            amount = int(str(self.amount.value).strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("Amount must be a positive whole number.", ephemeral=True)
            return

        client_seed = str(self.client_seed.value).strip()
        if not client_seed:
            await interaction.followup.send(
                "Your seed cannot be blank -- it is the half of the draw that is yours.",
                ephemeral=True,
            )
            return

        try:
            result = games.play(self.subject, self.game, self.selection, amount,
                                client_seed, commitment_id=self.commitment_id)
        except games.GameError as err:
            await interaction.followup.send(f"Could not place that bet: {err}", ephemeral=True)
            return
        except Exception as err:  # noqa: BLE001 -- money errors surface as plain refusals
            await interaction.followup.send(f"Could not place that bet: {err}", ephemeral=True)
            return

        embed = build_result_embed(result)
        await interaction.followup.send(embed=embed, view=RoundVerifyView(), ephemeral=True)


class CasinoPanelView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Play", style=discord.ButtonStyle.primary)
    async def play(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        game_options = [(label, key) for key, label in GAME_LABELS.items()]

        async def game_picked(inter: discord.Interaction, game: str) -> None:
            selection_options = SELECTION_LABELS[game]

            # Publish the commitment BEFORE the stake is known. `commit()`
            # writes its own transaction, so the hash below is durable and
            # auditable the moment the player reads it -- which is the whole
            # point: a hash shown after the bet proves nothing.
            try:
                published = games.commit(money.user(self.owner_id))
            except Exception as err:  # noqa: BLE001 -- refuse the round, don't half-open it
                await inter.response.send_message(
                    f"Could not open a round right now: {err}", ephemeral=True
                )
                return

            async def selection_picked(inter2: discord.Interaction, selection: str) -> None:
                await inter2.response.send_modal(
                    _BetModal(game, selection, money.user(self.owner_id),
                              published["commitment_id"])
                )

            await inter.response.send_message(
                f"Pick your {GAME_LABELS[game].lower()} selection.\n"
                f"Committed before you stake {SEP} server_seed_hash: "
                f"`{published['server_seed_hash']}`\n"
                f"Check it against the revealed seed with Verify after the round.",
                view=pickers.OptionPickerView(self.owner_id, selection_options, selection_picked),
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Pick a game:",
            view=pickers.OptionPickerView(self.owner_id, game_options, game_picked),
            ephemeral=True,
        )

    @discord.ui.button(label="My history", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        bets = queries.list_user_bets(money.user(self.owner_id))
        lines = [
            f"{b['game']}  {SEP} {b['selection']}  {SEP} staked "
            f"{money_text(b['amount'])}  {SEP} "
            f"{'won ' + money_text(b['payout_coins']) if b['payout_coins'] else 'lost'}"
            for b in bets if b["settled_event"] is not None
        ]
        await interaction.followup.send(
            embed=panel_embed("Your recent bets", rows(lines, empty_text="No bets yet.")),
            ephemeral=True,
        )

    @discord.ui.button(label="Verify a round", style=discord.ButtonStyle.secondary)
    async def verify_pick(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        rounds = queries.list_recent_rounds(limit=15)
        options = [(f"{r['game']} {SEP} {r['id']}"[:100], r["id"]) for r in rounds]

        async def picked(inter: discord.Interaction, round_id: str) -> None:
            if round_id == "_none":
                await inter.response.send_message("No settled rounds yet.", ephemeral=True)
                return
            await inter.response.send_message(embed=build_verify_embed(round_id), ephemeral=True)

        await interaction.response.send_message(
            "Pick a round to verify:",
            view=pickers.OptionPickerView(self.owner_id, options, picked),
            ephemeral=True,
        )
