"""core/predictions.py -- pari-mutuel prediction markets.

Players stake against each other on a shared outcome; the house does not take
the other side of the bet the way it does in `games.py`. It only ever takes an
explicit, configurable rake (`rake_bps`, default 0 -- "you take nothing" is a
locked default per the contract; a market only becomes a house edge if a
human deliberately sets a rake on it).

Lifecycle: `open_market` -> `stake` (places a hold, one per player position)
-> `close` (stops new stakes) -> `resolve` (pays out) or `void` (refunds
everyone, no market is ever a source of loss through error).

The remainder, and why it goes to the house
--------------------------------------------
Pro-rata division leaves a remainder: `distributable * stake // winning_pool`
floors for every winner, so `sum(payouts) <= distributable` almost always with
a strict `<`. Two places this could go: the house, or the largest stake.
This module sends it to the house (`treasury:games`, batched with the rake),
because:

  - it is the standard, boring choice (this is exactly how a horse-racing
    tote board rounds down payouts to the nearest unit and keeps the dust);
  - "largest stake" needs a tie-break rule for equal largest stakes, which is
    one more piece of surprising, hard-to-explain behaviour for a user who
    notices a few extra coins one way or the other;
  - it keeps the invariant trivial to state and test:
    `sum(payouts) + rake + remainder == pool`, always, by construction --
    `remainder` is *defined* as `pool - rake - sum(payouts)`, not computed
    separately and then reconciled.

If literally nobody staked the winning outcome, `winning_pool` is 0 and every
stake pays 0: the whole pool (rake and all) lands with the house. That is a
degenerate case of the same rule, not a special case of it.

Resolution mechanics
---------------------
`resolve` runs as ONE transaction, keyed on a caller-supplied event id
(`pred_markets.resolve_event`, UNIQUE -- a second resolve with a different
event id is refused loudly rather than silently replaying or double-paying).
Within that transaction:

  1. EVERY stake's hold is captured in full to `treasury:games` -- winners'
     and losers' alike. This is what "the pool" means concretely: the whole
     pool lands in the house's account first.
  2. Only then are winners paid, pro-rata, out of `treasury:games`.

Captures happen before any payout in the same call specifically so a
resolution never needs `treasury:games` to carry a working deficit just to
pay a market that is, by construction, self-funding.

Each stake's `settled_event` is set in the very statement that decides its
payout (claim-first, guarded by `WHERE settled_event IS NULL`), so if this
function is ever invoked twice for the same market it advances only the rows
that have not already been claimed -- never a re-pay.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import audit, money, wagering
from .db import db_in

SERVICE = "games"
TREASURY = money.SERVICE_TREASURY[SERVICE]  # "treasury:games"


class MarketError(RuntimeError):
    """Base class. A refusal, never a partial resolution."""


class UnknownMarket(MarketError): pass
class UnknownOutcome(MarketError): pass
class MarketNotOpen(MarketError): pass
class MarketVoided(MarketError): pass
class AlreadyResolved(MarketError): pass


class WagerRefused(MarketError):
    """`stake()` was refused by the SAME wagering guard coinflip/dice use --
    MAX_BET, MAX_DAILY_LOSS, or MIN_ACCOUNT_AGE_DAYS (see core/wagering.py).
    A MarketError subclass on purpose: bot/views/predict.py already catches
    `predictions.MarketError` around `stake()` and shows the player the
    refusal text, so this needs no new handling there."""


# ------------------------------------------------------------------ helpers

def _market(c: sqlite3.Connection, market_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM pred_markets WHERE id = ?", (market_id,)).fetchone()
    if row is None:
        raise UnknownMarket(market_id)
    return row


def _outcome_id(c: sqlite3.Connection, market_id: int, label: str) -> int:
    row = c.execute(
        "SELECT id FROM pred_outcomes WHERE market_id = ? AND label = ?", (market_id, label)
    ).fetchone()
    if row is None:
        raise UnknownOutcome(f"{label!r} is not an outcome of market {market_id}")
    return row["id"]


# ------------------------------------------------------------------ lifecycle

def open_market(question: str, outcomes: list[str], *, created_by: str,
                 rake_bps: int = 0, closes_at: str | None = None,
                 conn: Optional[sqlite3.Connection] = None) -> int:
    if len(outcomes) < 2:
        raise MarketError("a market needs at least two outcomes")
    if len(set(outcomes)) != len(outcomes):
        raise MarketError("outcome labels must be distinct")
    with db_in(conn) as c:
        cur = c.execute(
            "INSERT INTO pred_markets (question, rake_bps, closes_at, created_by) "
            "VALUES (?, ?, ?, ?)",
            (question, rake_bps, closes_at, created_by),
        )
        market_id = cur.lastrowid
        for label in outcomes:
            c.execute(
                "INSERT INTO pred_outcomes (market_id, label) VALUES (?, ?)",
                (market_id, label),
            )
    return market_id


def stake(market_id: int, subject: str, outcome_label: str, amount: int, *,
          conn: Optional[sqlite3.Connection] = None) -> int:
    """Place a hold against `subject` for a position on `outcome_label`.

    Routed through the SAME wagering guard `games.place_bet` uses
    (`core/wagering.check_wager`) -- MIN_ACCOUNT_AGE_DAYS, MAX_BET,
    MAX_DAILY_LOSS all apply to a prediction stake exactly as they do to a
    coinflip bet. `gambling_blocked` is enforced by `money.place_hold`
    itself (this stake's hold uses service="games", same as casino bets) and
    is not duplicated here.
    """
    with db_in(conn) as c:
        market = _market(c, market_id)
        if market["status"] != "open":
            raise MarketNotOpen(f"market {market_id} is {market['status']}, not open")
        outcome_id = _outcome_id(c, market_id, outcome_label)

        try:
            wagering.check_wager(c, subject, amount, kind="predictions", service=SERVICE)
        except wagering.WageringError as err:
            raise WagerRefused(str(err)) from err

        hold_id = money.place_hold(
            subject, amount, service=SERVICE,
            reason=f"stake on market {market_id}: {outcome_label}", conn=c,
        )
        c.execute(
            "INSERT INTO gambling_day (subject, day, staked, lost) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(subject, day) DO UPDATE SET staked = staked + excluded.staked",
            (subject, wagering.today(), amount),
        )
        cur = c.execute(
            "INSERT INTO pred_stakes (market_id, outcome_id, subject, amount, hold_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (market_id, outcome_id, subject, amount, hold_id),
        )
        stake_id = cur.lastrowid
    return stake_id


def close(market_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Stop new stakes. Does not decide anything -- resolve/void still do."""
    with db_in(conn) as c:
        _market(c, market_id)
        cur = c.execute(
            "UPDATE pred_markets SET status = 'closed' WHERE id = ? AND status = 'open'",
            (market_id,),
        )
        if cur.rowcount != 1:
            raise MarketNotOpen(f"market {market_id} could not be closed (not open)")


def void(market_id: int, *, actor: str = "unknown",
         conn: Optional[sqlite3.Connection] = None) -> int:
    """Release every open hold on the market. Nobody loses money on a voided
    market -- refund, not settlement. Idempotent: re-voiding an already-voided
    market simply finds nothing left to release.

    `actor` should be the staff member who voided this market -- pass
    `money.user(interaction.user.id)` from the caller. Defaults to
    "unknown" because the current admin panel (bot/views/admin.py's
    `_VoidConfirmModal`) does not yet capture or forward the resolver's
    identity; that is a bot/ gap to close, not something this module can fix
    on its own, so the audit row is honest about not knowing until it does.
    """
    with db_in(conn) as c:
        market = _market(c, market_id)
        if market["status"] == "resolved":
            raise AlreadyResolved(f"market {market_id} already resolved; cannot void")

        stakes = c.execute(
            "SELECT * FROM pred_stakes WHERE market_id = ? AND settled_event IS NULL",
            (market_id,),
        ).fetchall()

        released = 0
        ops: list[dict] = []
        for s in stakes:
            event_id = money.new_event_id("pred.void")
            claim_cur = c.execute(
                "UPDATE pred_stakes SET settled_event = ?, payout_coins = amount "
                "WHERE id = ? AND settled_event IS NULL",
                (event_id, s["id"]),
            )
            if claim_cur.rowcount != 1:
                continue
            money.release_hold(s["hold_id"], conn=c)
            ops.append({
                "op": "release_hold", "hold_id": s["hold_id"], "subject": s["subject"],
                "amount": s["amount"], "reverse": None,
            })
            released += 1

        c.execute("UPDATE pred_markets SET status = 'voided' WHERE id = ?", (market_id,))

        if released:
            audit.record(
                c, actor=actor, target=f"pred_market:{market_id}",
                kind="prediction.void",
                summary=f"voided market {market_id}: refunded {released} stake(s)",
                ops=ops, money_coins=0, manual_coins=0,
            )
    return released


def resolve(market_id: int, outcome_label: str, event_id: str, *,
            actor: str = "unknown",
            conn: Optional[sqlite3.Connection] = None) -> dict:
    """Resolve `market_id` to `outcome_label`, keyed on `event_id`.

    `event_id` must be minted at the source with `money.new_event_id(...)` --
    never reconstructed from a timestamp. Calling this again with the SAME
    event id on an already-resolved market is a safe replay (returns the
    original summary); calling it again with a DIFFERENT event id is refused
    loudly rather than silently ignored or re-run.

    `actor` should be the staff member who resolved this market -- pass
    `money.user(interaction.user.id)` from the caller. Defaults to
    "unknown" because the current admin panel (bot/views/admin.py's
    `_ResolveConfirmModal`) captures the resolver on `self.resolver` but
    never forwards it into this call; that is a bot/ gap to close, not
    something this module can fix on its own, so the audit row is honest
    about not knowing until it does.
    """
    with db_in(conn) as c:
        market = _market(c, market_id)

        if market["status"] == "voided":
            raise MarketVoided(f"market {market_id} was voided")

        if market["status"] == "resolved":
            if market["resolve_event"] == event_id:
                return _resolution_summary(c, market_id)
            raise AlreadyResolved(
                f"market {market_id} already resolved by a different event"
            )

        if market["status"] not in ("open", "closed"):
            raise MarketNotOpen(market["status"])

        winning_outcome_id = _outcome_id(c, market_id, outcome_label)

        c.execute(
            "UPDATE pred_markets SET status = 'resolved', resolved_outcome_id = ?, "
            "resolve_event = ? WHERE id = ? AND status != 'resolved'",
            (winning_outcome_id, event_id, market_id),
        )
        market = _market(c, market_id)
        if market["resolve_event"] != event_id:
            # a concurrent resolve won the race between our read and our write
            raise AlreadyResolved(
                f"market {market_id} was resolved concurrently by a different event"
            )

        pool = c.execute(
            "SELECT COALESCE(SUM(amount), 0) AS n FROM pred_stakes WHERE market_id = ?",
            (market_id,),
        ).fetchone()["n"]
        winning_pool = c.execute(
            "SELECT COALESCE(SUM(amount), 0) AS n FROM pred_stakes "
            "WHERE market_id = ? AND outcome_id = ?",
            (market_id, winning_outcome_id),
        ).fetchone()["n"]

        rake_bps = market["rake_bps"]
        rake = pool * rake_bps // 10_000
        distributable = pool - rake

        stakes = c.execute(
            "SELECT * FROM pred_stakes WHERE market_id = ? AND settled_event IS NULL",
            (market_id,),
        ).fetchall()

        money.ensure_wallet(TREASURY, conn=c)

        # Pass 1: capture EVERY stake (winners and losers alike) into the
        # house first. This is what "the pool" means concretely, and it means
        # paying winners in pass 2 never needs the house to carry a deficit.
        claimed: list[tuple[str, int, int, str]] = []
        ops: list[dict] = []
        for s in stakes:
            is_winner = s["outcome_id"] == winning_outcome_id and winning_pool > 0
            payout = (distributable * s["amount"] // winning_pool) if is_winner else 0
            row_event = money.new_event_id("pred.settle")

            claim_row = c.execute(
                "UPDATE pred_stakes SET settled_event = ?, payout_coins = ? "
                "WHERE id = ? AND settled_event IS NULL",
                (row_event, payout, s["id"]),
            )
            if claim_row.rowcount != 1:
                continue  # already settled by a previous call

            money.capture_hold(
                s["hold_id"], service=SERVICE, reason=f"market {market_id} resolved",
                to=TREASURY, ref_kind="pred_market", ref_id=str(market_id),
                idem_key=row_event, conn=c,
            )
            ops.append({
                "op": "capture_hold", "hold_id": s["hold_id"], "subject": s["subject"],
                "amount": s["amount"], "to": TREASURY,
                "reverse": {"op": "transfer", "src": TREASURY, "dst": s["subject"],
                            "amount": s["amount"]},
            })
            claimed.append((s["subject"], s["amount"], payout, row_event))

        # Pass 2: pay winners pro-rata out of what pass 1 just collected, and
        # write each stake's NET result (stake - payout, floored at 0) into
        # gambling_day -- the same daily-loss ledger core/games.py's bets
        # use, so a prediction loss counts against MAX_DAILY_LOSS exactly
        # like a coinflip loss does, instead of staying invisible to it.
        paid_out = 0
        for subject, staked, payout, row_event in claimed:
            if payout > 0:
                money.transfer(
                    TREASURY, subject, payout, service=SERVICE,
                    reason=f"market {market_id} payout",
                    ref_kind="pred_market", ref_id=str(market_id),
                    idem_key=row_event, conn=c,
                )
                paid_out += payout
                ops.append({
                    "op": "transfer", "src": TREASURY, "dst": subject, "amount": payout,
                    "reverse": {"op": "transfer", "src": subject, "dst": TREASURY,
                                "amount": payout},
                })
            net_loss = staked - payout
            if net_loss > 0:
                wagering.record_loss(c, subject, net_loss)

        if claimed:
            audit.record(
                c, actor=actor, target=f"pred_market:{market_id}",
                kind="prediction.resolve",
                summary=(
                    f"resolved market {market_id} to {outcome_label!r}: pool {pool:,}, "
                    f"paid {paid_out:,}, rake {rake:,}, remainder {pool - rake - paid_out:,}"
                ),
                ops=ops, money_coins=pool + paid_out, manual_coins=0,
                action_key=f"audit:pred.resolve:{event_id}",
            )

    return {
        "market_id": market_id,
        "pool": pool,
        "rake": rake,
        "winning_pool": winning_pool,
        "paid_out": paid_out,
        "remainder": pool - rake - paid_out,
    }


def _resolution_summary(c: sqlite3.Connection, market_id: int) -> dict:
    market = c.execute("SELECT * FROM pred_markets WHERE id = ?", (market_id,)).fetchone()
    pool = c.execute(
        "SELECT COALESCE(SUM(amount), 0) AS n FROM pred_stakes WHERE market_id = ?",
        (market_id,),
    ).fetchone()["n"]
    winning_pool = c.execute(
        "SELECT COALESCE(SUM(amount), 0) AS n FROM pred_stakes "
        "WHERE market_id = ? AND outcome_id = ?",
        (market_id, market["resolved_outcome_id"]),
    ).fetchone()["n"]
    paid_out = c.execute(
        "SELECT COALESCE(SUM(payout_coins), 0) AS n FROM pred_stakes "
        "WHERE market_id = ? AND payout_coins IS NOT NULL",
        (market_id,),
    ).fetchone()["n"]
    rake = pool * market["rake_bps"] // 10_000
    return {
        "market_id": market_id, "pool": pool, "rake": rake, "winning_pool": winning_pool,
        "paid_out": paid_out, "remainder": pool - rake - paid_out,
    }
