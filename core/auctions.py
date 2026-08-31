"""core/auctions.py -- public open-bid (English) auctions on catalog items.

Not a wager: nobody stakes money against an uncertain outcome for nothing.
A bid places a hold for its FULL amount; the moment a higher bid supersedes
it, the previous leader's hold is released back to them, so at every point
in time a bidder is either the current leader (money escrowed, item
theirs if nothing outbids them) or holding nothing at all. Because it is
money-safe by construction -- nobody can lose more than the price of the
lot they actually win -- bidding does NOT run through core/wagering.py's
MAX_BET/MAX_DAILY_LOSS guard, which exists to cap real RISK OF LOSS, not
every hold in the system. `orders_blocked` gates participation instead of
`gambling_blocked`: bidding on a lot is commerce, same family as an order,
not a wager (CONTRACT.md section 9's Discord-only wagering line is about
games.py/predictions.py specifically and does not reach this module).
`money.place_hold` only auto-enforces `gambling_blocked` for services in
`money.GAMBLING_SERVICES`, so `orders_blocked` is checked explicitly here.

Lifecycle: `open_auction` (staff, picks a real catalog item -- never a
typed name) -> `bid` (repeatable; each bid is a fresh hold, releasing
the previous leader's) -> `close` (closes_at reached, stops new bids) ->
`settle` (captures the winning hold to `treasury:shop`, or settles with no
winner if nobody bid). `close` and `settle` both run automatically off
`sweep_expired`, called by a loop in bot/cogs/admin.py the moment
`closes_at` passes. Unlike a prediction market, an auction's outcome is the
objective top bid at close, not a staff judgement call, so there is no
insider-window risk in settling it the instant it closes and no reason to
put a human in that loop.

`void` (staff-only, before settlement) releases the current lead's hold, if
any, and cancels the listing -- the escape hatch for a lot listed by
mistake. It never touches `stock` or `items`: an auction is a MONEY event
only. Handing over the physical item is a staff task, exactly like an
order's delivery -- there is no in-game inventory in this system for a
lot to move through automatically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import audit, money
from .db import db_in

SERVICE = "shop"
TREASURY = money.SERVICE_TREASURY[SERVICE]  # "treasury:shop"


class AuctionError(RuntimeError):
    """Base class. A refusal, never a partial apply."""


class UnknownAuction(AuctionError): pass
class UnknownItem(AuctionError): pass
class AuctionNotOpen(AuctionError): pass
class AuctionStillOpen(AuctionError): pass
class AlreadySettled(AuctionError): pass
class BidTooLow(AuctionError): pass


class OrdersBlocked(AuctionError):
    """`subject` has the `orders_blocked` wallet flag. A commerce-side
    refusal, not `money.GamblingBlocked` -- bidding is not a wager, so it is
    checked here rather than by `money.place_hold`'s built-in gate, which
    only fires for `money.GAMBLING_SERVICES`."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _auction(c: sqlite3.Connection, auction_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
    if row is None:
        raise UnknownAuction(auction_id)
    return row


def _leading_bid(c: sqlite3.Connection, auction_id: int) -> Optional[sqlite3.Row]:
    return c.execute(
        "SELECT * FROM auction_bids WHERE auction_id = ? AND status = 'active' "
        "ORDER BY amount DESC, id ASC LIMIT 1",
        (auction_id,),
    ).fetchone()


# ------------------------------------------------------------------ lifecycle

def open_auction(item_id: int, pieces: int, min_bid: int, min_increment: int,
                  duration_minutes: int, *, created_by: str,
                  conn: Optional[sqlite3.Connection] = None) -> int:
    if pieces <= 0:
        raise AuctionError("pieces must be positive")
    if min_bid <= 0:
        raise AuctionError("min_bid must be positive")
    if min_increment <= 0:
        raise AuctionError("min_increment must be positive")
    if duration_minutes <= 0:
        raise AuctionError("duration_minutes must be positive")
    with db_in(conn) as c:
        item = c.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise UnknownItem(item_id)
        closes_at = (datetime.now(timezone.utc)
                     + timedelta(minutes=duration_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        cur = c.execute(
            "INSERT INTO auctions (item_id, pieces, min_bid, min_increment, "
            "created_by, closes_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, pieces, min_bid, min_increment, created_by, closes_at),
        )
        return cur.lastrowid


def bid(auction_id: int, subject: str, amount: int, *,
        conn: Optional[sqlite3.Connection] = None) -> int:
    """Place a bid. Refused if the auction is not open, has already passed
    its close time (checked here too, not just by the sweep -- the sweep
    runs on an interval and must never be the only thing standing between a
    late bid and a lot that should already be closed), or `amount` does not
    clear the minimum: `min_bid` with no bids yet, or the current lead plus
    `min_increment` otherwise.

    Order matters, same shape as `predictions.stake`: the new hold is
    placed FIRST (so an unaffordable bid fails with nothing else touched),
    and only once that succeeds does the previous leader's hold get
    released and marked `outbid`. A challenger who cannot afford their bid
    must never cost the current leader their escrow.
    """
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise AuctionError("amount must be a positive int")
    with db_in(conn) as c:
        auction = _auction(c, auction_id)
        if auction["status"] != "open":
            raise AuctionNotOpen(f"auction {auction_id} is {auction['status']}, not open")
        if auction["closes_at"] <= _now():
            raise AuctionNotOpen(f"auction {auction_id} has already reached its close time")

        if "orders_blocked" in money.flags(subject, conn=c):
            raise OrdersBlocked(f"{subject} has orders blocked")

        leader = _leading_bid(c, auction_id)
        floor = auction["min_bid"] if leader is None else leader["amount"] + auction["min_increment"]
        if amount < floor:
            raise BidTooLow(f"bid must be at least {floor:,}, got {amount:,}")

        hold_id = money.place_hold(
            subject, amount, service=SERVICE,
            reason=f"bid on auction {auction_id}", conn=c,
        )

        if leader is not None:
            c.execute(
                "UPDATE auction_bids SET status = 'outbid' WHERE id = ?", (leader["id"],)
            )
            money.release_hold(leader["hold_id"], conn=c)

        cur = c.execute(
            "INSERT INTO auction_bids (auction_id, subject, amount, hold_id) "
            "VALUES (?, ?, ?, ?)",
            (auction_id, subject, amount, hold_id),
        )
        return cur.lastrowid


def set_message(auction_id: int, channel_id: str, message_id: str, *,
                 conn: Optional[sqlite3.Connection] = None) -> None:
    """Record which Discord message carries this auction's public card.

    The card is posted after `open_auction` returns, so the ids arrive
    later. A persistent view re-resolves the auction FROM the message it is
    on, so without this the card can never be refreshed as bids land or the
    lot settles."""
    with db_in(conn) as c:
        c.execute("UPDATE auctions SET channel_id = ?, message_id = ? WHERE id = ?",
                  (str(channel_id), str(message_id), int(auction_id)))


def close(auction_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Stop new bids. Does not decide anything -- settle/void still do."""
    with db_in(conn) as c:
        _auction(c, auction_id)
        cur = c.execute(
            "UPDATE auctions SET status = 'closed' WHERE id = ? AND status = 'open'",
            (auction_id,),
        )
        if cur.rowcount != 1:
            raise AuctionNotOpen(f"auction {auction_id} could not be closed (not open)")


def settle(auction_id: int, event_id: str, *, actor: str = "system:sweep",
           conn: Optional[sqlite3.Connection] = None) -> dict:
    """Settle `auction_id`, keyed on `event_id` -- same replay contract as
    `predictions.resolve`: calling this again with the SAME event id on an
    already-settled auction is a safe replay; a DIFFERENT event id is
    refused loudly.

    Requires the auction already CLOSED (see `close()`), so the top-bid
    read below can never race a bid still landing.
    """
    with db_in(conn) as c:
        auction = _auction(c, auction_id)

        if auction["status"] == "voided":
            raise AuctionError(f"auction {auction_id} was voided")

        if auction["status"] == "settled":
            if auction["settle_event"] == event_id:
                return _settlement_summary(auction)
            raise AlreadySettled(f"auction {auction_id} already settled by a different event")

        if auction["status"] != "closed":
            raise AuctionStillOpen(
                f"auction {auction_id} is {auction['status']}, not closed -- "
                "call close() before settle()"
            )

        leader = _leading_bid(c, auction_id)
        winner = leader["subject"] if leader is not None else None
        winning_amount = leader["amount"] if leader is not None else None

        cur = c.execute(
            "UPDATE auctions SET status = 'settled', winner = ?, winning_amount = ?, "
            "settle_event = ?, settled_at = ? "
            "WHERE id = ? AND status != 'settled'",
            (winner, winning_amount, event_id, _now(), auction_id),
        )
        auction = _auction(c, auction_id)
        if auction["settle_event"] != event_id:
            # a concurrent settle won the race between our read and our write
            raise AlreadySettled(f"auction {auction_id} was settled concurrently")

        ops: list[dict] = []
        if leader is not None:
            money.ensure_wallet(TREASURY, conn=c)
            money.capture_hold(
                leader["hold_id"], service=SERVICE, reason=f"auction {auction_id} settled",
                to=TREASURY, ref_kind="auction", ref_id=str(auction_id),
                idem_key=event_id, conn=c,
            )
            c.execute("UPDATE auction_bids SET status = 'won' WHERE id = ?", (leader["id"],))
            ops.append({
                "op": "capture_hold", "hold_id": leader["hold_id"], "subject": leader["subject"],
                "amount": leader["amount"], "to": TREASURY,
                "reverse": {"op": "transfer", "src": TREASURY, "dst": leader["subject"],
                            "amount": leader["amount"]},
            })
            summary = f"auction {auction_id} settled: won by {winner} at {winning_amount:,}"
        else:
            summary = f"auction {auction_id} settled: no bids"

        audit.record(
            c, actor=actor, target=f"auction:{auction_id}", kind="auction.settle",
            summary=summary, ops=ops, money_coins=(winning_amount or 0), manual_coins=0,
            action_key=f"audit:auction.settle:{event_id}",
        )
    return _settlement_summary(auction)


def void(auction_id: int, *, actor: str = "unknown",
         conn: Optional[sqlite3.Connection] = None) -> bool:
    """Cancel a listing before settlement. Releases the current leader's
    hold, if any -- nobody loses money on a voided auction. Idempotent in
    spirit: a second void on an already-voided auction is refused, same as
    `predictions.void`'s market-status guards, because there is nothing left
    to release and no reason to pretend it did something."""
    with db_in(conn) as c:
        auction = _auction(c, auction_id)
        if auction["status"] == "settled":
            raise AlreadySettled(f"auction {auction_id} already settled; cannot void")
        if auction["status"] == "voided":
            return False

        leader = _leading_bid(c, auction_id)
        ops: list[dict] = []
        if leader is not None:
            c.execute(
                "UPDATE auction_bids SET status = 'refunded' WHERE id = ?", (leader["id"],)
            )
            money.release_hold(leader["hold_id"], conn=c)
            ops.append({
                "op": "release_hold", "hold_id": leader["hold_id"], "subject": leader["subject"],
                "amount": leader["amount"], "reverse": None,
            })

        c.execute("UPDATE auctions SET status = 'voided' WHERE id = ?", (auction_id,))
        audit.record(
            c, actor=actor, target=f"auction:{auction_id}", kind="auction.void",
            summary=f"voided auction {auction_id}"
                    + (f": released {leader['subject']}'s bid" if leader is not None else ""),
            ops=ops, money_coins=0, manual_coins=0,
        )
    return True


def sweep_expired(*, conn: Optional[sqlite3.Connection] = None) -> list[int]:
    """Close and settle every open auction whose `closes_at` has passed.
    Called on an interval by bot/cogs/admin.py -- an auction's winner is the
    objective top bid at close, not a staff judgement call, so there is no
    reason to make a human close and settle it by hand the way a prediction
    market's resolve is (CONTRACT.md section 9's insider-window argument for
    staff-gated resolution does not apply here).

    Each auction gets its OWN transaction and its own freshly-minted event
    id, so one bad row never blocks the rest of the sweep."""
    with db_in(conn) as c:
        due = c.execute(
            "SELECT id FROM auctions WHERE status = 'open' AND closes_at <= ?", (_now(),)
        ).fetchall()
        auction_ids = [row["id"] for row in due]

    settled: list[int] = []
    for auction_id in auction_ids:
        try:
            close(auction_id)
            settle(auction_id, money.new_event_id("auction.settle"))
        except AuctionError:
            continue
        settled.append(auction_id)
    return settled


def _settlement_summary(auction: sqlite3.Row) -> dict:
    return {
        "auction_id": auction["id"],
        "status": auction["status"],
        "winner": auction["winner"],
        "winning_amount": auction["winning_amount"],
    }
