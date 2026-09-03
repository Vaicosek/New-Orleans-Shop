"""core/land.py -- staff-listed plot sales: public open-bid (English)
auctions with an optional instant buy-now price.

Same money contract as core/auctions.py, deliberately mirrored rather than
reused: a bid places a hold for its FULL amount, and the moment a higher
bid supersedes it the previous leader's hold is released, so at every
point in time a bidder is either the current leader (money escrowed, plot
theirs if nothing outbids them) or holding nothing at all. Not a wager --
`orders_blocked` gates participation, not `money.GamblingBlocked` (see
core/auctions.py's docstring for the full reasoning; it applies here
unchanged).

A listing here is a hand-typed plot -- name, description, a free-text
location -- not a row in `items`: there is no catalog entry, no stock
count, and no in-game inventory for a plot to move through. This is
deliberately the SIMPLE version: no chunk-claim-mod integration, no AI
valuation, no stock-market tie-in. Staff types a description; buyers bid
on the description; staff hands the plot over in-game by hand, exactly
like an auction lot or an order's delivery. See CONTRACT.md section 11a.

Lifecycle: `open_listing` (staff, free-text plot details) -> `bid`
(repeatable; instantly settles the listing if `amount` clears
`buy_now_price`) -> `close` (closes_at reached, stops new bids) ->
`settle` (captures the winning hold to `treasury:shop`, or settles with no
winner if nobody bid). `close`/`settle` also run automatically off
`sweep_expired`, on the same one-minute loop as auctions
(bot/cogs/admin.py). A buy-now bid decides its own outcome the instant it
clears the price -- same "no insider-window" reasoning as an auction's
close, so settling it immediately, in the same transaction as the bid,
needs no human in the loop either.

`void` (staff-only, before settlement) releases the current lead's hold,
if any, and cancels the listing -- the escape hatch for a plot listed by
mistake. It is a MONEY event only, same as void_auction.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import audit, money
from .db import db_in

SERVICE = "shop"
TREASURY = money.SERVICE_TREASURY[SERVICE]  # "treasury:shop"


class LandError(RuntimeError):
    """Base class. A refusal, never a partial apply."""


class UnknownListing(LandError): pass
class ListingNotOpen(LandError): pass
class ListingStillOpen(LandError): pass
class AlreadySettled(LandError): pass
class BidTooLow(LandError): pass


class OrdersBlocked(LandError):
    """`subject` has the `orders_blocked` wallet flag. Commerce, not a
    wager -- same reasoning as core/auctions.py's OrdersBlocked."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _listing(c: sqlite3.Connection, land_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM land_listings WHERE id = ?", (land_id,)).fetchone()
    if row is None:
        raise UnknownListing(land_id)
    return row


def _leading_bid(c: sqlite3.Connection, land_id: int) -> Optional[sqlite3.Row]:
    return c.execute(
        "SELECT * FROM land_bids WHERE land_id = ? AND status = 'active' "
        "ORDER BY amount DESC, id ASC LIMIT 1",
        (land_id,),
    ).fetchone()


# ------------------------------------------------------------------ lifecycle

def open_listing(name: str, description: str, location: str, min_bid: int,
                  min_increment: int, duration_minutes: int, *, created_by: str,
                  buy_now_price: Optional[int] = None, rent_coins: int = 0,
                  conn: Optional[sqlite3.Connection] = None) -> int:
    if not name or not name.strip():
        raise LandError("name is required")
    if min_bid <= 0:
        raise LandError("min_bid must be positive")
    if min_increment <= 0:
        raise LandError("min_increment must be positive")
    if duration_minutes <= 0:
        raise LandError("duration_minutes must be positive")
    if buy_now_price is not None and buy_now_price < min_bid:
        raise LandError("buy_now_price cannot be below min_bid")
    if rent_coins < 0:
        raise LandError("rent_coins cannot be negative")
    with db_in(conn) as c:
        closes_at = (datetime.now(timezone.utc)
                     + timedelta(minutes=duration_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        cur = c.execute(
            "INSERT INTO land_listings (name, description, location, min_bid, "
            "min_increment, buy_now_price, rent_coins, created_by, closes_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), description.strip(), location.strip(), min_bid, min_increment,
             buy_now_price, rent_coins, created_by, closes_at),
        )
        return cur.lastrowid


# ------------------------------------------------------------------ stalls: rent

RENT_PERIOD_DAYS = 30


def _period_for(settled_at: str, now: datetime) -> tuple[int, str]:
    """Which rent period `now` falls in, counting from the settlement, and
    that period's label. Period 0 is the deposit month (paid by the winning
    bid); rent is owed from period 1 on."""
    start = datetime.strptime(settled_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    n = max((now - start).days // RENT_PERIOD_DAYS, 0)
    return n, f"p{n}"


def sweep_rent(*, conn: Optional[sqlite3.Connection] = None) -> dict[str, list[int]]:
    """Charge rent on every let stall whose current period is unpaid; vacate
    the ones whose tenant cannot pay.

    A stall is a settled listing with `rent_coins > 0`, a winner, and no
    `vacated_at`. The winning bid was the deposit and covers period 0; from
    period 1 the tenant owes `rent_coins` per RENT_PERIOD_DAYS, paid to
    `treasury:shop` -- recurring income from space in the shop's own
    district, which is scarce by definition on a server where land is not.

    Idempotent per period: `land_rent` is UNIQUE on (land_id, period), so
    this can run every minute and charge once a period. A tenant who cannot
    cover the rent is vacated rather than run into debt -- the deficit floor
    on a player wallet is 0 and must stay 0; a stall is not worth a
    negative balance. Each stall is its own transaction, one bad row never
    blocking the rest, like every other sweep here.
    """
    now = datetime.now(timezone.utc)
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    charged: list[int] = []
    vacated: list[int] = []
    with db_in(conn) as c:
        stalls = c.execute(
            "SELECT id, winner, rent_coins, settled_at FROM land_listings "
            " WHERE status = 'settled' AND rent_coins > 0 AND winner IS NOT NULL "
            "   AND vacated_at IS NULL AND settled_at IS NOT NULL"
        ).fetchall()
    for s in stalls:
        try:
            n, period = _period_for(s["settled_at"], now)
            if n < 1:
                continue                                    # still inside the deposit month
            with db_in(conn) as c:
                already = c.execute(
                    "SELECT 1 FROM land_rent WHERE land_id = ? AND period = ?",
                    (s["id"], period),
                ).fetchone()
                if already:
                    continue
                available = money.balance(s["winner"], conn=c).available
                if available < int(s["rent_coins"]):
                    c.execute(
                        "UPDATE land_listings SET vacated_at = ? WHERE id = ? AND vacated_at IS NULL",
                        (now_s, s["id"]),
                    )
                    audit.record(
                        c, actor="system", target=f"land:{s['id']}", kind="land.vacate",
                        summary=(f"stall {s['id']} vacated: {s['winner']} could not cover "
                                 f"{s['rent_coins']:,} rent for {period}"),
                        ops=[], money_coins=0, manual_coins=0,
                    )
                    vacated.append(s["id"])
                    continue
                event_id = money.new_event_id("rent")
                money.transfer(
                    s["winner"], TREASURY, int(s["rent_coins"]), service=SERVICE,
                    reason=f"stall {s['id']} rent, period {period}",
                    ref_kind="land", ref_id=str(s["id"]), idem_key=event_id, conn=c,
                )
                c.execute(
                    "INSERT INTO land_rent (land_id, tenant, period, coins, paid_event) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (s["id"], s["winner"], period, int(s["rent_coins"]), event_id),
                )
                charged.append(s["id"])
        except Exception:  # noqa: BLE001 -- one stall never blocks the sweep
            continue
    return {"charged": charged, "vacated": vacated}


def bid(land_id: int, subject: str, amount: int, *,
        conn: Optional[sqlite3.Connection] = None) -> dict:
    """Place a bid. Refused if the listing is not open, has already passed
    its close time, or `amount` does not clear the minimum -- same ordering
    as core/auctions.py's `bid`: the new hold is placed FIRST, and only
    once that succeeds does the previous leader's hold get released and
    marked `outbid`.

    If `amount` clears `buy_now_price`, the listing is closed and settled
    to this bid in the SAME transaction -- there is no window between the
    winning bid landing and the sale being final. Returns
    {"bid_id": ..., "bought_now": bool, "settlement": dict | None}.
    """
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise LandError("amount must be a positive int")
    with db_in(conn) as c:
        listing = _listing(c, land_id)
        if listing["status"] != "open":
            raise ListingNotOpen(f"listing {land_id} is {listing['status']}, not open")
        if listing["closes_at"] <= _now():
            raise ListingNotOpen(f"listing {land_id} has already reached its close time")

        if "orders_blocked" in money.flags(subject, conn=c):
            raise OrdersBlocked(f"{subject} has orders blocked")

        leader = _leading_bid(c, land_id)
        floor = listing["min_bid"] if leader is None else leader["amount"] + listing["min_increment"]
        if amount < floor:
            raise BidTooLow(f"bid must be at least {floor:,}, got {amount:,}")

        hold_id = money.place_hold(
            subject, amount, service=SERVICE,
            reason=f"bid on land listing {land_id}", conn=c,
        )

        if leader is not None:
            c.execute(
                "UPDATE land_bids SET status = 'outbid' WHERE id = ?", (leader["id"],)
            )
            money.release_hold(leader["hold_id"], conn=c)

        cur = c.execute(
            "INSERT INTO land_bids (land_id, subject, amount, hold_id) "
            "VALUES (?, ?, ?, ?)",
            (land_id, subject, amount, hold_id),
        )
        bid_id = cur.lastrowid

        bought_now = listing["buy_now_price"] is not None and amount >= listing["buy_now_price"]
        settlement = None
        if bought_now:
            c.execute(
                "UPDATE land_listings SET status = 'closed' WHERE id = ? AND status = 'open'",
                (land_id,),
            )
            settlement = settle(land_id, money.new_event_id("land.buynow"),
                                 actor=subject, conn=c)

        return {"bid_id": bid_id, "bought_now": bought_now, "settlement": settlement}


def set_message(land_id: int, channel_id: str, message_id: str, *,
                 conn: Optional[sqlite3.Connection] = None) -> None:
    """Record which Discord message carries this listing's public card --
    same reasoning as core/auctions.py's `set_message`."""
    with db_in(conn) as c:
        c.execute("UPDATE land_listings SET channel_id = ?, message_id = ? WHERE id = ?",
                  (str(channel_id), str(message_id), int(land_id)))


def close(land_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Stop new bids. Does not decide anything -- settle/void still do."""
    with db_in(conn) as c:
        _listing(c, land_id)
        cur = c.execute(
            "UPDATE land_listings SET status = 'closed' WHERE id = ? AND status = 'open'",
            (land_id,),
        )
        if cur.rowcount != 1:
            raise ListingNotOpen(f"listing {land_id} could not be closed (not open)")


def settle(land_id: int, event_id: str, *, actor: str = "system:sweep",
           conn: Optional[sqlite3.Connection] = None) -> dict:
    """Settle `land_id`, keyed on `event_id` -- same replay contract as
    core/auctions.py's `settle`: calling this again with the SAME event id
    on an already-settled listing is a safe replay; a DIFFERENT event id is
    refused loudly. Requires the listing already CLOSED."""
    with db_in(conn) as c:
        listing = _listing(c, land_id)

        if listing["status"] == "voided":
            raise LandError(f"listing {land_id} was voided")

        if listing["status"] == "settled":
            if listing["settle_event"] == event_id:
                return _settlement_summary(listing)
            raise AlreadySettled(f"listing {land_id} already settled by a different event")

        if listing["status"] != "closed":
            raise ListingStillOpen(
                f"listing {land_id} is {listing['status']}, not closed -- "
                "call close() before settle()"
            )

        leader = _leading_bid(c, land_id)
        winner = leader["subject"] if leader is not None else None
        winning_amount = leader["amount"] if leader is not None else None

        c.execute(
            "UPDATE land_listings SET status = 'settled', winner = ?, winning_amount = ?, "
            "settle_event = ?, settled_at = ? "
            "WHERE id = ? AND status != 'settled'",
            (winner, winning_amount, event_id, _now(), land_id),
        )
        listing = _listing(c, land_id)
        if listing["settle_event"] != event_id:
            # a concurrent settle won the race between our read and our write
            raise AlreadySettled(f"listing {land_id} was settled concurrently")

        ops: list[dict] = []
        if leader is not None:
            money.ensure_wallet(TREASURY, conn=c)
            money.capture_hold(
                leader["hold_id"], service=SERVICE, reason=f"land listing {land_id} settled",
                to=TREASURY, ref_kind="land", ref_id=str(land_id),
                idem_key=event_id, conn=c,
            )
            c.execute("UPDATE land_bids SET status = 'won' WHERE id = ?", (leader["id"],))
            ops.append({
                "op": "capture_hold", "hold_id": leader["hold_id"], "subject": leader["subject"],
                "amount": leader["amount"], "to": TREASURY,
                "reverse": {"op": "transfer", "src": TREASURY, "dst": leader["subject"],
                            "amount": leader["amount"]},
            })
            summary = f"listing {land_id} settled: won by {winner} at {winning_amount:,}"
        else:
            summary = f"listing {land_id} settled: no bids"

        audit.record(
            c, actor=actor, target=f"land:{land_id}", kind="land.settle",
            summary=summary, ops=ops, money_coins=(winning_amount or 0), manual_coins=0,
            action_key=f"audit:land.settle:{event_id}",
        )
    return _settlement_summary(listing)


def void(land_id: int, *, actor: str = "unknown",
         conn: Optional[sqlite3.Connection] = None) -> bool:
    """Cancel a listing before settlement. Releases the current leader's
    hold, if any -- nobody loses money on a voided listing. Same idempotent
    shape as core/auctions.py's `void`."""
    with db_in(conn) as c:
        listing = _listing(c, land_id)
        if listing["status"] == "settled":
            raise AlreadySettled(f"listing {land_id} already settled; cannot void")
        if listing["status"] == "voided":
            return False

        leader = _leading_bid(c, land_id)
        ops: list[dict] = []
        if leader is not None:
            c.execute(
                "UPDATE land_bids SET status = 'refunded' WHERE id = ?", (leader["id"],)
            )
            money.release_hold(leader["hold_id"], conn=c)
            ops.append({
                "op": "release_hold", "hold_id": leader["hold_id"], "subject": leader["subject"],
                "amount": leader["amount"], "reverse": None,
            })

        c.execute("UPDATE land_listings SET status = 'voided' WHERE id = ?", (land_id,))
        audit.record(
            c, actor=actor, target=f"land:{land_id}", kind="land.void",
            summary=f"voided land listing {land_id}"
                    + (f": released {leader['subject']}'s bid" if leader is not None else ""),
            ops=ops, money_coins=0, manual_coins=0,
        )
    return True


def sweep_expired(*, conn: Optional[sqlite3.Connection] = None) -> list[int]:
    """Close and settle every open listing whose `closes_at` has passed.
    Same reasoning and same one-bad-row-never-blocks-the-rest shape as
    core/auctions.py's `sweep_expired` -- called by the same loop."""
    with db_in(conn) as c:
        due = c.execute(
            "SELECT id FROM land_listings WHERE status = 'open' AND closes_at <= ?", (_now(),)
        ).fetchall()
        land_ids = [row["id"] for row in due]

    settled: list[int] = []
    for land_id in land_ids:
        try:
            close(land_id)
            settle(land_id, money.new_event_id("land.settle"))
        except LandError:
            continue
        settled.append(land_id)
    return settled


def _settlement_summary(listing: sqlite3.Row) -> dict:
    return {
        "land_id": listing["id"],
        "status": listing["status"],
        "winner": listing["winner"],
        "winning_amount": listing["winning_amount"],
    }
