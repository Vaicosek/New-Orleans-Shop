"""Casino panel: game picker, bet, round history, fairness verify.

Coinflip, dice and slots, per CONTRACT.md section 9. Every round result
carries a persistent "Verify" button -- registered once at boot with no
round id attached, re-resolving the round from the message it is on -- so a
bet made before a restart can still be checked afterwards.
"""
from __future__ import annotations

import json
import re

import discord

from core import games, money

from . import pickers
from .. import addressing, queries
from ..ui.embed import SEP, money_text, panel_embed, rows

_ROUND_MARK = re.compile(r"round:(\S+)")

GAME_LABELS = {"coinflip": "Coinflip", "dice": "Dice", "slots": "Slots"}
SELECTION_LABELS = {
    "coinflip": [("Heads", "heads"), ("Tails", "tails")],
    "dice": [(f"Roll {n}", str(n)) for n in range(1, 7)],
    # Slots has nothing to predict, only to spin -- one dummy selection so
    # it settles through the same call shape as every other game (see
    # core.games._payout_bps). CasinoPanelView.play skips the picker step
    # entirely when there is only one option, so this never costs a click.
    "slots": [("Spin", "spin")],
}

_SLOT_SYMBOLS = {"cherry": "🍒", "lemon": "🍋",
                 "bell": "🔔", "seven": "7️⃣"}


def _reels_text(reels: list[str]) -> str:
    return " ".join(_SLOT_SYMBOLS.get(r, r) for r in reels)


def _round_footer(round_id: str, code: str) -> str:
    return f"address {code}  {SEP} round:{round_id}"


def _short_hex(value: str, *, head: int = 12, tail: int = 6) -> str:
    """Shorten a long hex blob to a glance-form ("a1b2c3...9f8e"). Below the
    head+tail length there is nothing to save by truncating."""
    if not value or len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def parse_round_id(message: discord.Message | None) -> str | None:
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer
    text = getattr(footer, "text", None)
    if not text:
        return None
    m = _ROUND_MARK.search(text)
    return m.group(1) if m else None


def _outcome_text(game: str, outcome: dict) -> str:
    if game == "coinflip":
        return outcome.get("face")
    if game == "dice":
        return f"rolled {outcome.get('roll')}"
    return _reels_text(outcome.get("reels", []))  # slots


def build_result_embed(result: dict) -> discord.Embed:
    bet = result["results"][0] if result["results"] else None
    game = result["game"]
    outcome = result["outcome"]
    outcome_text = _outcome_text(game, outcome)
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


# Plain-words label for each of verify()'s four independent checks, in the
# same order games.verify()'s docstring lists them. All four render every
# time -- a tampered round used to show only two of these (seed vs outcome)
# and call itself INVALID with no way to tell a player which of the four
# actually broke.
_VERIFY_CHECKS = [
    ("seed_matches_commitment", "Revealed seed hashes to the committed hash"),
    ("commitment_matches_round", "Round was played against that same commitment, unchanged"),
    ("committed_before_bets", "Commitment existed before the first bet"),
    ("outcome_matches", "Outcome recomputes from the seed, client seed and nonce"),
]


def build_verify_embed(round_id: str) -> discord.Embed:
    try:
        v = games.verify(round_id)
    except games.GameError as err:
        return panel_embed("Verify round", f"Could not verify: {err}")

    failed_labels = []
    check_lines = []
    for key, label in _VERIFY_CHECKS:
        passed = v[key]
        check_lines.append(f"{'PASS' if passed else 'FAIL'} {SEP} {label}")
        if not passed:
            failed_labels.append(label)

    verdict = "VALID" if v["ok"] else "INVALID"
    lines = [f"Overall: {verdict}", ""] + check_lines
    if failed_labels:
        lines += ["", "Failed: " + "; ".join(failed_labels)]
    # The pre-bet commitment hash -- from the COMMITMENT row, never the
    # round's own copy -- is the value a player who saved the commitment
    # message should be comparing against.
    committed_hash = v["commitment_server_seed_hash"]
    server_seed = v["server_seed"]
    if committed_hash:
        hash_line = (f"committed server_seed_hash: {_short_hex(committed_hash)}"
                     f"  (full: {committed_hash})")
    else:
        hash_line = "committed server_seed_hash: (no commitment on record)"
    lines += [
        "",
        f"server_seed: {_short_hex(server_seed)}  (full: {server_seed})",
        hash_line,
        f"client_seed: {v['client_seed']}",
        f"nonce: {v['nonce']}",
    ]
    body = "\n".join(lines)
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
                 commitment_id: str | None = None, server_seed_hash: str | None = None):
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
        # This bet's own fresh commitment hash, shown here because a Modal
        # cannot show free text and this interaction's single response is
        # `send_modal` -- there is nowhere else to put it that is still
        # current for THIS bet (see defects 8/14). Not required, and the
        # player is told not to edit it: it is display-only, carried on the
        # TextInput's default so it is still visible and copyable.
        if server_seed_hash:
            self.commitment_hash = discord.ui.TextInput(
                label="Committed hash (yours to verify, don't edit)",
                default=_short_hex(server_seed_hash),
                required=False,
                max_length=100,
            )
            self.add_item(self.commitment_hash)

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
        except games.SeedSecretError:
            # Deliberately NOT caught as a refusal (see core.games.SeedSecretError's
            # own docstring): a missing/placeholder/short seed secret means the whole
            # casino's fairness is compromised, not that this one bet failed. It must
            # reach the process boundary and crash the process loudly rather than be
            # shown to the player as a routine "your bet didn't go through" -- that
            # silently hid the exact condition this exception exists to surface.
            raise
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

            async def selection_picked(inter2: discord.Interaction, selection: str) -> None:
                # Mint a FRESH commitment per bet, right here, immediately
                # before opening the modal -- not once per game-pick. A
                # commitment is single-use: the first bet's settle_round()
                # flips it open -> revealed, so reusing one closure'd
                # commitment across every selection from the same dropdown
                # made every bet after the first fail with "not open"
                # (defect 8). Minting it here also means the hash the
                # player is shown (inside the modal, defect 14) is always
                # the hash THIS bet actually settles against, never stale.
                try:
                    published = games.commit(money.user(self.owner_id))
                except games.SeedSecretError:
                    # Same reasoning as _BetModal.on_submit: this is a deployment
                    # defect, not a normal per-round refusal, and must crash loudly
                    # rather than be shown to the player as "try again".
                    raise
                except Exception as err:  # noqa: BLE001 -- refuse the round, don't half-open it
                    await inter2.response.send_message(
                        f"Could not open a round right now: {err}", ephemeral=True
                    )
                    return
                await inter2.response.send_modal(
                    _BetModal(game, selection, money.user(self.owner_id),
                              published["commitment_id"], published["server_seed_hash"])
                )

            # Slots has exactly one selection ("spin") -- nothing to choose,
            # so making the player pick it from a one-item dropdown would be
            # a click that exists only because the code shape wanted it. Go
            # straight to selection_picked on the SAME interaction (its
            # .response is still unused here, same as it would be inside
            # the picker's own callback) whenever there is only one option;
            # any future single-selection game gets this for free too.
            if len(selection_options) == 1:
                await selection_picked(inter, selection_options[0][1])
                return

            await inter.response.send_message(
                f"Pick your {GAME_LABELS[game].lower()} selection.\n"
                f"Each bet gets its own fresh commitment -- its hash will show on "
                f"the bet form itself. Check it against the revealed seed with "
                f"Verify after the round.",
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

        def _outcome_text(b: dict) -> str:
            # Three real outcomes, not two: a voided round (core.games._void_round)
            # refunds the stake in full and stores that refund in payout_coins too
            # -- so `payout_coins` truthy does not mean "won". Every win pays out
            # strictly more than the stake (every payout core.games._payout_bps can
            # return for a win exceeds 10,000 bps, slots' smallest included), so the
            # stake itself is the dividing line: 0 is a loss, exactly the stake back
            # is a void/refund, anything above the stake is a win.
            payout = b["payout_coins"]
            if payout <= 0:
                return "lost"
            if payout <= b["amount"]:
                return "voided, refunded"
            return "won " + money_text(payout)

        lines = [
            f"{b['game']}  {SEP} {b['selection']}  {SEP} staked "
            f"{money_text(b['amount'])}  {SEP} {_outcome_text(b)}"
            for b in bets if b["settled_event"] is not None
        ]
        await interaction.followup.send(
            embed=panel_embed("Your recent bets", rows(lines, empty_text="No bets yet.")),
            ephemeral=True,
        )

    @discord.ui.button(label="Verify a round", style=discord.ButtonStyle.secondary)
    async def verify_pick(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        rounds = queries.list_recent_rounds(limit=15)

        def _round_label(r: dict) -> str:
            # Player-facing label: game name, when it settled, and the
            # outcome if we can read one out of outcome_json -- never the
            # raw round id or the raw internal game key. The id still goes
            # in the option VALUE (the user never sees that), same pattern
            # as admin.py's "Resolve market" picker.
            game_label = GAME_LABELS.get(r["game"], r["game"])
            bits = [game_label]
            settled_at = r.get("settled_at")
            if settled_at:
                bits.append(str(settled_at))
            outcome_json = r.get("outcome_json")
            if outcome_json:
                try:
                    outcome = json.loads(outcome_json)
                except (TypeError, ValueError):
                    outcome = None
                if isinstance(outcome, dict):
                    if outcome.get("face") is not None:
                        bits.append(f"result {outcome['face']}")
                    elif outcome.get("roll") is not None:
                        bits.append(f"rolled {outcome['roll']}")
                    elif outcome.get("reels") is not None:
                        bits.append(_reels_text(outcome["reels"]))
            return f" {SEP} ".join(bits)[:100]

        options = [(_round_label(r), r["id"]) for r in rounds]

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
