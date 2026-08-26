"""core/games.py -- house-banked, provably-fair casino games.

Two games only, on purpose: coinflip and dice. Both share one commit/reveal
scheme and one settlement path.

Provably fair, in one paragraph
--------------------------------
Before a round is played, the house commits to a secret `server_seed` by
publishing only `sha256(server_seed)` (`game_rounds.server_seed_hash`). The
seed itself is never written to the row while the round is open -- the CHECK
constraint on `game_rounds` enforces that a row can only be 'settled' once
`server_seed` and `outcome_json` both exist. Rather than stash the real secret
somewhere else for the gap between commit and reveal, it is *derived*
deterministically from a persistent process secret plus the round id
(`_server_seed_for`): nobody without that process secret can predict it ahead
of time, and once it is written into the row at settlement it is a fully
public value. `verify()` recomputes everything from that public row alone --
it never touches the process secret -- so anyone can audit a settled round.

The process secret comes from NOLA_GAME_SEED_SECRET and is validated by
`configure()` / `_check_seed_secret()` -- refused if missing, if it is the
placeholder literal that used to ship as this module's default, or if it is
too short. If the secret a running process derives from no longer matches a
round's `server_seed_hash` (the process restarted with the env var rotated
in between commit and reveal, most likely), `settle_round` cannot recover a
fair outcome for that round -- it voids the round and refunds every open bet
in full rather than raising and stranding the holds.

The outcome itself is `HMAC(server_seed, f"{client_seed}:{nonce}")`: the
player supplies `client_seed` (so the house alone cannot have pre-selected a
losing outcome for them), the house supplies `server_seed` (so the player
alone cannot grind outcomes by resubmitting client seeds).

House edge -- explicit, not emergent
-------------------------------------
Every game's edge is a config number (`GAME_CONFIG[...]["payout_bps"]`), not a
side effect of the payout arithmetic. Moving it means changing one number and
updating the comment next to it, on purpose -- it must never be possible for
the actual edge to silently drift from the documented one.

    coinflip: win chance 1/2, fair multiplier 2.00x, pays 1.96x  -> edge  2%
    dice:     win chance 1/6, fair multiplier 6.00x, pays 5.70x  -> edge  5%

Money flow -- house-banked, never mints
-----------------------------------------
A bet is a hold at placement (`place_bet`), never a debit. `gambling_blocked`
is enforced by `money.place_hold` at that moment; this module does not (and
must not) re-check the flag itself.

Before a bet is even accepted, `place_bet` checks that `treasury:games`
could fund the BEST case for the player (see `_treasury_capacity` /
`HouseInsolvent`) -- refusing up front, with a clear reason, is what keeps
the house from ever having to decide a win it cannot pay.

On settlement (`settle_round`):
  - lose: the hold is captured in full to `treasury:games`.
  - win:  the hold is released (the player's own stake was never anyone
          else's money, so it simply becomes available again) and the
          *profit* (payout - stake) is transferred from `treasury:games` to
          the player. `treasury:games` has TRANSFER and HOLD scope only --
          no MINT -- so a bug here can misallocate the house's bankroll and
          can never conjure new coins.

          If the treasury still runs dry between the check above and this
          transfer (concurrent bets can do that even though each one passed
          its own pre-check), the round is NEVER rolled back for it: an
          unpayable win must never be reported to the player as a failed
          bet, and `game_rounds`/`game_bets` must never be destroyed just
          because the house's cash position moved. Instead the profit is
          recorded as a debt -- one `audit_actions` row with `manual_coins`
          set to what is owed and `reversed_at` NULL until a human pays it
          by hand (`audit.pending_debts()` lists every one outstanding) --
          and the round settles normally otherwise. The player's stake is
          still returned in full either way.

Settlement is idempotent per bet: `game_bets.settled_event` is UNIQUE and is
set in the very statement that decides the payout amount, guarded by
`WHERE settled_event IS NULL`. Calling `settle_round` again -- on a round
already settled, or on one where some bets settled and others didn't -- only
ever touches bets that haven't been claimed yet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from . import audit, money, wagering
from .pricing import CURRENCY
from .db import db, db_in

SERVICE = "games"
TREASURY = money.SERVICE_TREASURY[SERVICE]  # "treasury:games"

# ------------------------------------------------------------------ config
# Explicit numbers. Never derive a limit or an edge from the payout maths.
#
# These three are re-exported from core/wagering.py, the ONE guard shared
# with core/predictions.py -- keep importing them from here (games.MAX_BET
# etc. is the public name bot/views/casino.py and the tests use), just don't
# redefine them here, or the two modules drift again.

MAX_BET = wagering.MAX_BET
MAX_DAILY_LOSS = wagering.MAX_DAILY_LOSS
MIN_ACCOUNT_AGE_DAYS = wagering.MIN_ACCOUNT_AGE_DAYS

GAME_CONFIG: dict[str, dict[str, Any]] = {
    "coinflip": {
        "selections": ("heads", "tails"),
        # win pays 1.96x stake. Fair (P=1/2) would be 2.00x.
        # edge = 1 - 1.96/2.00 = 2%
        "payout_bps": 19_600,
    },
    "dice": {
        "selections": ("1", "2", "3", "4", "5", "6"),
        # win pays 5.70x stake. Fair (P=1/6) would be 6.00x.
        # edge = 1 - 5.70/6.00 = 5%
        "payout_bps": 57_000,
    },
}


class GameError(RuntimeError):
    """Base class. A refusal, never a partial settlement."""


class UnknownGame(GameError): pass
class UnknownRound(GameError): pass
class RoundNotOpen(GameError): pass
class BadSelection(GameError): pass
class BetTooLarge(GameError): pass
class DailyLossExceeded(GameError): pass
class AccountTooNew(GameError): pass


class HouseInsolvent(GameError):
    """`treasury:games` cannot currently fund a win on this bet even in the
    best case. Refused BEFORE the hold is placed -- a bet that could not be
    paid if it won must never be accepted in the first place. (A win that
    slips past this and still can't be funded at settlement -- because the
    treasury moved between placing the bet and settling the round -- is not
    an error either: see `settle_round`'s pending-payout path.)"""


class SeedSecretError(RuntimeError):
    """NOLA_GAME_SEED_SECRET is missing, is still the shipped placeholder, or
    is too short to carry real entropy.

    Deliberately NOT a GameError: a GameError is a normal per-bet refusal
    that bot/views/casino.py catches and shows the player as "your bet
    failed". This is a deployment defect, not a bet outcome, and must not be
    swallowed by that handling -- it needs to reach the process boundary and
    crash the process loudly. This runs on hosts with no shell to read a
    traceback off of, so the message itself has to be the whole diagnosis:
    which env var, and the exact command to generate a value for it.
    """


# ------------------------------------------------------------------ helpers

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _treasury_capacity(c: sqlite3.Connection) -> int:
    """How many more coins `treasury:games` could pay out right now without
    a `money.InsufficientFunds` -- its available balance plus how far its
    deficit floor still lets it go negative. Ensures the wallet exists first
    (it may not, this early -- `bootstrap_treasuries` runs at boot, but a
    test or a fresh DB may call this before that has happened)."""
    money.ensure_wallet(TREASURY, conn=c)
    bal = money.balance(TREASURY, conn=c)
    floor_row = c.execute(
        "SELECT deficit_floor FROM wallets WHERE subject = ?", (TREASURY,)
    ).fetchone()
    return bal.available + int(floor_row["deficit_floor"])


_INSECURE_DEFAULT_SECRET = "dev-insecure-seed-secret-change-me"
MIN_SEED_SECRET_LENGTH = 20  # chars -- rules out a short, guessable literal

_HOW_TO_GENERATE_SECRET = 'python -c "import secrets; print(secrets.token_hex(32))"'


def _check_seed_secret(secret: str | None) -> str:
    """The one place that decides whether a secret is real. Every failure
    names the exact env var and the exact command to generate a value for
    it -- there is no shell on the host to expand on a bare traceback."""
    if not secret:
        raise SeedSecretError(
            "NOLA_GAME_SEED_SECRET is not set. The casino cannot run without "
            "a real, private server-seed secret -- without one, the seed for "
            "every round is guessable from the source alone. Generate one "
            f"with:\n    {_HOW_TO_GENERATE_SECRET}\n"
            "then set NOLA_GAME_SEED_SECRET to that value before starting "
            "this process."
        )
    if secret == _INSECURE_DEFAULT_SECRET:
        raise SeedSecretError(
            "NOLA_GAME_SEED_SECRET is set to the placeholder value shipped "
            "in core/games.py's own source. That value is public -- anyone "
            "who has read the code (or this repo, if it is ever public) can "
            "predict every round. Generate a real secret with:\n"
            f"    {_HOW_TO_GENERATE_SECRET}\n"
            "and set NOLA_GAME_SEED_SECRET to that value."
        )
    if len(secret) < MIN_SEED_SECRET_LENGTH:
        raise SeedSecretError(
            f"NOLA_GAME_SEED_SECRET is only {len(secret)} character(s); at "
            f"least {MIN_SEED_SECRET_LENGTH} are required to carry enough "
            f"entropy to resist guessing. Generate one with:\n"
            f"    {_HOW_TO_GENERATE_SECRET}"
        )
    return secret


def configure() -> None:
    """Boot-time check: refuse to start the casino without a real secret.

    Call this once at process boot -- see bot/main.py's `build_bot()`, which
    runs it right alongside `load_bot_config()`, well before any cog loads
    or any round opens. That is the load-bearing check: failing here means
    an operator sees ONE clear FATAL line and the process exits, instead of
    the casino quietly running on the public default until a player notices
    they cannot lose.

    `_server_seed_for` below also calls `_check_seed_secret` on every
    derivation, so a caller that skips this boot check still cannot get an
    insecure round played -- but that path only fails at the first bet,
    which is too late to be the primary signal.
    """
    _check_seed_secret(os.environ.get("NOLA_GAME_SEED_SECRET"))


def _server_seed_for(round_id: str) -> str:
    """Deterministic from a persistent process secret + round_id. Never
    stored anywhere while the round is open -- regenerating it needs the
    secret; verifying it (after reveal) needs only the public row."""
    secret = _check_seed_secret(os.environ.get("NOLA_GAME_SEED_SECRET"))
    return hmac.new(secret.encode(), round_id.encode(), hashlib.sha256).hexdigest()


def _outcome(game: str, server_seed: str, client_seed: str, nonce: int) -> dict:
    if game not in GAME_CONFIG:
        raise UnknownGame(game)
    mix = f"{client_seed}:{nonce}".encode()
    digest = hmac.new(server_seed.encode(), mix, hashlib.sha256).digest()
    if game == "coinflip":
        return {"face": "heads" if digest[0] % 2 == 0 else "tails"}
    return {"roll": digest[0] % 6 + 1}  # dice


def _wins(game: str, selection: str, outcome: dict) -> bool:
    if game == "coinflip":
        return selection == outcome["face"]
    return selection == str(outcome["roll"])  # dice


# ------------------------------------------------------------------ rounds

def open_round(game: str, client_seed: str, nonce: int = 0, *,
                round_id: str | None = None,
                conn: Optional[sqlite3.Connection] = None) -> str:
    """Commit to a round. `server_seed_hash` is written now; `server_seed`
    stays NULL until `settle_round` reveals it."""
    if game not in GAME_CONFIG:
        raise UnknownGame(game)
    round_id = round_id or money.new_event_id(f"round.{game}")
    server_seed = _server_seed_for(round_id)
    seed_hash = hashlib.sha256(server_seed.encode()).hexdigest()
    with db_in(conn) as c:
        c.execute(
            "INSERT INTO game_rounds (id, game, server_seed_hash, client_seed, nonce) "
            "VALUES (?, ?, ?, ?, ?)",
            (round_id, game, seed_hash, client_seed, nonce),
        )
    return round_id


def place_bet(round_id: str, subject: str, selection: str, amount: int, *,
              conn: Optional[sqlite3.Connection] = None) -> int:
    """Escrow `amount` against `subject` for a bet on `round_id`.

    All limits are enforced here, server-side, before the hold is placed:
    MAX_BET, MAX_DAILY_LOSS (realized loss from `gambling_day` PLUS this
    subject's currently-open games exposure -- see the comment at the check
    below), MIN_ACCOUNT_AGE_DAYS. `gambling_blocked` is enforced by
    `money.place_hold` itself and is not duplicated here.
    """
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM game_rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            raise UnknownRound(round_id)
        if row["state"] != "open":
            raise RoundNotOpen(f"round {round_id} is {row['state']}, not open")

        game = row["game"]
        cfg = GAME_CONFIG[game]
        if selection not in cfg["selections"]:
            raise BadSelection(f"{selection!r} is not valid for {game}")

        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("amount must be a positive int")
        if amount > MAX_BET:
            raise BetTooLarge(f"{amount:,} exceeds MAX_BET {MAX_BET:,}")

        # Solvency, checked BEFORE the bet is accepted at all: refuse a bet
        # up front, with a clear reason, if the house could not fund it even
        # in the best case for the player. Refusing here is what stops an
        # unpayable win from ever being decided in the first place -- see
        # settle_round's pending-payout path for the (rarer, concurrent-bet)
        # case where the treasury still runs dry between this check and
        # settlement despite passing here.
        profit_if_win = (amount * cfg["payout_bps"]) // 10_000 - amount
        if profit_if_win > 0 and _treasury_capacity(c) < profit_if_win:
            raise HouseInsolvent(
                f"the house cannot currently fund a win on this {amount:,} {CURRENCY} "
                f"{game} bet (would owe {profit_if_win:,} more on top of the "
                f"stake back); try a smaller amount or again later"
            )

        # MIN_ACCOUNT_AGE_DAYS, MAX_DAILY_LOSS (realized loss from
        # gambling_day PLUS this subject's currently-open games exposure) --
        # the ONE guard shared with predictions.stake(). Re-raised as this
        # module's own exception classes so callers (bot/views/casino.py,
        # tests) keep seeing games.BetTooLarge/DailyLossExceeded/AccountTooNew
        # exactly as before; only the check itself moved.
        try:
            wagering.check_wager(c, subject, amount, kind="games", service=SERVICE)
        except wagering.AccountTooNew as err:
            raise AccountTooNew(str(err)) from err
        except wagering.BetTooLarge as err:
            raise BetTooLarge(str(err)) from err
        except wagering.DailyLossExceeded as err:
            raise DailyLossExceeded(str(err)) from err

        today = _today()

        # Placing the hold is the money-committing step; it validates funds,
        # freeze state and gambling_blocked all in one WHERE clause.
        hold_id = money.place_hold(
            subject, amount, service=SERVICE, reason=f"{game} bet on {selection}", conn=c
        )

        c.execute(
            "INSERT INTO gambling_day (subject, day, staked, lost) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(subject, day) DO UPDATE SET staked = staked + excluded.staked",
            (subject, today, amount),
        )
        cur = c.execute(
            "INSERT INTO game_bets (round_id, subject, amount, selection, hold_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (round_id, subject, amount, selection, hold_id),
        )
        bet_id = cur.lastrowid
    return bet_id


def _void_round(c: sqlite3.Connection, round_id: str, game: str, reason: str) -> dict:
    """Refund every not-yet-settled bet on `round_id` in full and close the
    round as 'voided'. Mirrors predictions.void(): a refund is recorded as a
    hold RELEASE, never a capture -- nobody's money is captured for a round
    that never produced a fair, verifiable outcome. A voided round holds no
    server_seed and no outcome_json (the schema's CHECK constraint on
    game_rounds forbids it for any non-'settled' state) -- there is no
    fair outcome to reveal for it, and there never will be."""
    bets = c.execute(
        "SELECT * FROM game_bets WHERE round_id = ? AND settled_event IS NULL",
        (round_id,),
    ).fetchall()
    results = []
    for bet in bets:
        event_id = money.new_event_id("game.void")
        claim_cur = c.execute(
            "UPDATE game_bets SET settled_event = ?, payout_coins = amount "
            "WHERE id = ? AND settled_event IS NULL",
            (event_id, bet["id"]),
        )
        if claim_cur.rowcount != 1:
            continue  # a previous call already settled/voided this bet
        money.release_hold(bet["hold_id"], conn=c)
        results.append({
            "bet_id": bet["id"], "subject": bet["subject"], "selection": bet["selection"],
            "win": None, "payout": bet["amount"], "voided": True,
        })
    c.execute(
        "UPDATE game_rounds SET state = 'voided' WHERE id = ? AND state = 'open'",
        (round_id,),
    )
    return {"round_id": round_id, "game": game, "voided": True, "reason": reason,
            "results": results}


def settle_round(round_id: str, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Reveal the seed (if not already revealed) and settle every bet on the
    round that hasn't been settled yet.

    Idempotent: calling this twice on the same round pays exactly once. The
    round-level reveal and the per-bet payout decision are two independent
    claims (`game_rounds.state`, `game_bets.settled_event`), so a retry after
    a partial failure only ever redoes the part that didn't commit.
    """
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM game_rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            raise UnknownRound(round_id)
        if row["state"] == "voided":
            raise RoundNotOpen(f"round {round_id} was voided")

        if row["state"] == "open":
            server_seed = _server_seed_for(round_id)
            if hashlib.sha256(server_seed.encode()).hexdigest() != row["server_seed_hash"]:
                # The seed this process derives right now does not match what
                # was committed when the round opened -- in practice, an
                # unpersisted restart with NOLA_GAME_SEED_SECRET rotated in
                # between. The real seed was never stored while the round was
                # open (that is the whole point of the commit/reveal scheme),
                # so there is no way to recover it or compute a fair outcome
                # any more. The only safe move is to void: refund every open
                # bet in full and close the round -- a player's hold must
                # never be stranded waiting on a secret this process cannot
                # reproduce.
                return _void_round(
                    c, round_id, row["game"],
                    "server seed no longer matches the round's committed "
                    "hash (NOLA_GAME_SEED_SECRET changed since this round "
                    "opened) -- round voided and all open bets refunded",
                )
            outcome = _outcome(row["game"], server_seed, row["client_seed"], row["nonce"])
            c.execute(
                "UPDATE game_rounds SET server_seed = ?, outcome_json = ?, "
                "state = 'settled', settled_at = datetime('now') "
                "WHERE id = ? AND state = 'open'",
                (server_seed, json.dumps(outcome), round_id),
            )
            row = c.execute("SELECT * FROM game_rounds WHERE id = ?", (round_id,)).fetchone()

        game = row["game"]
        outcome = json.loads(row["outcome_json"])
        cfg = GAME_CONFIG[game]

        money.ensure_wallet(TREASURY, conn=c)

        bets = c.execute(
            "SELECT * FROM game_bets WHERE round_id = ? AND settled_event IS NULL",
            (round_id,),
        ).fetchall()

        results = []
        moved_coins = 0      # moved automatically by this call (captures + paid profit)
        pending_coins = 0    # profit decided but not automatically payable -- a debt
        ops: list[dict] = []
        for bet in bets:
            win = _wins(game, bet["selection"], outcome)
            payout_total = (bet["amount"] * cfg["payout_bps"]) // 10_000 if win else 0
            event_id = money.new_event_id("game.settle")

            claim_cur = c.execute(
                "UPDATE game_bets SET settled_event = ?, payout_coins = ? "
                "WHERE id = ? AND settled_event IS NULL",
                (event_id, payout_total, bet["id"]),
            )
            if claim_cur.rowcount != 1:
                continue  # a previous call already settled this bet

            payout_pending = False
            if win:
                money.release_hold(bet["hold_id"], conn=c)
                profit = payout_total - bet["amount"]
                if profit < 0:
                    raise GameError(f"{game} configured to pay below stake on a win")
                if profit > 0:
                    try:
                        money.transfer(
                            TREASURY, bet["subject"], profit, service=SERVICE,
                            reason=f"{game} win, round {round_id}",
                            ref_kind="game_round", ref_id=round_id,
                            idem_key=event_id, conn=c,
                        )
                    except money.InsufficientFunds:
                        # The house cannot fund this win RIGHT NOW -- almost
                        # always a concurrent bet draining the treasury
                        # between place_bet's own pre-check and this moment.
                        # The round and this bet are NOT rolled back for it:
                        # the player won, the round is real and verifiable,
                        # and what is owed becomes a visible debt (see
                        # core/audit.py) rather than a vanished round.
                        payout_pending = True
                        pending_coins += profit
                        ops.append({
                            "op": "debt", "owed_to": bet["subject"], "amount": profit,
                            "reason": f"{game} win, round {round_id}: treasury:games "
                                      "could not fund this automatically",
                            "reverse": None,
                        })
                    else:
                        moved_coins += profit
                        ops.append({
                            "op": "transfer", "src": TREASURY, "dst": bet["subject"],
                            "amount": profit,
                            "reverse": {"op": "transfer", "src": bet["subject"],
                                        "dst": TREASURY, "amount": profit},
                        })
                loss_amount = 0
            else:
                money.capture_hold(
                    bet["hold_id"], service=SERVICE, reason=f"{game} loss, round {round_id}",
                    to=TREASURY, ref_kind="game_round", ref_id=round_id,
                    idem_key=event_id, conn=c,
                )
                moved_coins += bet["amount"]
                ops.append({
                    "op": "capture_hold", "hold_id": bet["hold_id"], "subject": bet["subject"],
                    "amount": bet["amount"], "to": TREASURY,
                    "reverse": {"op": "transfer", "src": TREASURY, "dst": bet["subject"],
                                "amount": bet["amount"]},
                })
                loss_amount = bet["amount"]

            if loss_amount:
                wagering.record_loss(c, bet["subject"], loss_amount)

            results.append({
                "bet_id": bet["id"], "subject": bet["subject"], "selection": bet["selection"],
                "win": win, "payout": payout_total, "payout_pending": payout_pending,
            })

        if results:
            # One audit row per settle_round call that actually claimed
            # work -- a replay that finds nothing left to settle writes
            # nothing, matching the "one row per action" rule without
            # needing a dedup key: two calls can never claim the same bet.
            audit.record(
                c, actor="system:games", target=f"game_round:{round_id}",
                kind="game.settle",
                summary=(
                    f"{game} round {round_id}: settled {len(results)} bet(s), "
                    f"moved {moved_coins:,} {CURRENCY} automatically"
                    + (f", {pending_coins:,} {CURRENCY} owed as a pending payout"
                       if pending_coins else "")
                ),
                ops=ops, money_coins=moved_coins, manual_coins=pending_coins,
            )

    return {"round_id": round_id, "game": row["game"], "outcome": outcome, "results": results}


def play(subject: str, game: str, selection: str, amount: int, client_seed: str,
         nonce: int = 0) -> dict:
    """One-shot: commit a round, place a single bet, settle immediately.

    Coinflip and dice are house games -- there is no external event to wait
    for, so the whole commit/bet/reveal cycle is one atomic transaction. This
    is the normal path a player takes; `open_round`/`place_bet`/`settle_round`
    exist separately for tests and for anything that wants to inspect state
    between steps.
    """
    with db() as c:
        round_id = open_round(game, client_seed, nonce, conn=c)
        place_bet(round_id, subject, selection, amount, conn=c)
        return settle_round(round_id, conn=c)


def verify(round_id: str, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Recompute a settled round from public values alone. No secret is
    needed here -- `server_seed` has already been revealed on the row."""
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM game_rounds WHERE id = ?", (round_id,)).fetchone()
    if row is None:
        raise UnknownRound(round_id)
    if row["state"] != "settled":
        raise RoundNotOpen(f"round {round_id} is {row['state']}, not settled -- nothing to verify")

    seed_matches = hashlib.sha256(row["server_seed"].encode()).hexdigest() == row["server_seed_hash"]
    recomputed = _outcome(row["game"], row["server_seed"], row["client_seed"], row["nonce"])
    stored = json.loads(row["outcome_json"])

    return {
        "round_id": round_id,
        "game": row["game"],
        "seed_matches_commitment": seed_matches,
        "outcome_matches": recomputed == stored,
        "ok": seed_matches and recomputed == stored,
        "server_seed": row["server_seed"],
        "server_seed_hash": row["server_seed_hash"],
        "client_seed": row["client_seed"],
        "nonce": row["nonce"],
        "outcome": stored,
    }
