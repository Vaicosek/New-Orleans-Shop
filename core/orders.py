"""Order lifecycle: open -> claimed -> awaiting_verification -> fulfilled
(or cancelled).

Every payout rule here is carried straight from `CONTRACT.md` section 8 and
`core/money.py`'s own doctrine, because an order payout IS a money move and
gets no exemption from those rules:

- one atomic UPDATE ... WHERE, judged by rowcount, never read-then-write
  (`claim`, `mark_fulfilled`'s delivery bound, `approve`'s pay-gate).
- claim first, then act: `order_claims.UNIQUE(order_id, worker)` plus the
  remaining-pieces check happen in the SAME INSERT ... SELECT ... WHERE that
  creates the claim, so a losing concurrent claimer never overwrites work
  that already won.
- a zero or missing price is a loud failure at payout, never a silent
  zero-coin payment -- a corrupted snapshot must never look like a
  successful payout.
- `order_claims.paid_event` (UNIQUE) is set inside the SAME UPDATE that
  decides to pay a claim, which is the actual double-pay guard; `approve()`
  merely respects it rather than re-implementing it.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from . import audit, money
from .db import db_in
from .money import normalise_subject
from .pricing import CURRENCY, split_charge


class OrderError(RuntimeError):
    """Base class. Every failure here is a refusal, never a partial apply."""


class NoSuchOrder(OrderError):
    pass


class NoSuchItem(OrderError):
    pass


class NotClaimable(OrderError):
    """Order is not in a status that permits the attempted transition."""


class AlreadyClaimed(OrderError):
    """This worker already holds a claim on this order (UNIQUE(order_id, worker))."""


class InsufficientRemaining(OrderError):
    """Fewer unclaimed pieces remain than requested."""


class NoSuchClaim(OrderError):
    pass


class OverDelivery(OrderError):
    """Delivered pieces would exceed what this worker actually claimed."""


class ZeroPrice(OrderError):
    """The order's snapshotted price is zero or missing. Refuse, never pay 0."""


class SelfApproval(OrderError):
    """An approver who claimed or delivered this order may not approve it."""


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


# ------------------------------------------------------------------ lifecycle

def create_order(item_id: int, requested_pieces: int, created_by: str, *,
                  channel_id: str | None = None, message_id: str | None = None,
                  conn: Optional[sqlite3.Connection] = None) -> int:
    """Open an order, SNAPSHOTTING the item's current price and stack size
    onto the order row.

    The snapshot is the whole point: if the owner reprices the item five
    minutes from now, this order still charges -- and pays out -- at the
    price that was live when it was opened. `approve()` reads
    `orders.price_coins`/`orders.price_unit_pieces`, never the item's live
    columns, for exactly this reason.
    """
    requested_pieces = _positive_int(requested_pieces, "requested_pieces")
    created_by = normalise_subject(created_by)
    with db_in(conn) as c:
        item = c.execute(
            "SELECT price_coins, price_unit_pieces, stack_size, active FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if item is None:
            raise NoSuchItem(f"no such item {item_id}")
        if not item["active"]:
            raise NoSuchItem(f"item {item_id} is not active")
        cur = c.execute(
            "INSERT INTO orders (item_id, requested_pieces, price_coins, price_unit_pieces, "
            "stack_size, created_by, channel_id, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, requested_pieces, item["price_coins"], item["price_unit_pieces"],
             item["stack_size"], created_by, channel_id, message_id),
        )
        return cur.lastrowid


def get_order(order_id: int, *, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise NoSuchOrder(f"no such order {order_id}")
    return _row(row)


def list_claims(order_id: int, *,
                 conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT * FROM order_claims WHERE order_id = ? ORDER BY claimed_at", (order_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def set_message(order_id: int, channel_id: str, message_id: str, *,
                conn: Optional[sqlite3.Connection] = None) -> None:
    """Record which Discord message carries this order's card.

    The card is posted after `create_order` returns, so the ids arrive later.
    A persistent view re-resolves its subject FROM the message it is on, so
    without this the channel card can never be refreshed and an already-paid
    order keeps a live Approve button on it.
    """
    with db_in(conn) as c:
        c.execute("UPDATE orders SET channel_id = ?, message_id = ? WHERE id = ?",
                  (str(channel_id), str(message_id), int(order_id)))


def claim(order_id: int, worker: str, pieces: int, *,
          conn: Optional[sqlite3.Connection] = None) -> int:
    """Claim `pieces` pieces of `order_id` for `worker`.

    The guard is the database, not a read-then-write: `order_claims` has
    `UNIQUE(order_id, worker)`, and the remaining-unclaimed check is computed
    INSIDE the same INSERT ... SELECT ... WHERE via a correlated SUM. Two
    workers racing for the last N pieces both run this exact statement; a
    read-then-write version would let both see "room for N" before either
    writes and oversell the order, but here SQLite's writer serialization
    (this project's `db()` opens with BEGIN IMMEDIATE) means the second
    statement re-evaluates the SUM against the first one's already-committed
    row, so only one of them can still satisfy the WHERE.
    """
    pieces = _positive_int(pieces, "pieces")
    worker = normalise_subject(worker)
    with db_in(conn) as c:
        try:
            cur = c.execute(
                "INSERT INTO order_claims (order_id, worker, pieces) "
                "SELECT :oid, :worker, :pieces "
                "  FROM orders o "
                " WHERE o.id = :oid "
                "   AND o.status IN ('open', 'claimed') "
                "   AND :pieces <= o.requested_pieces - COALESCE("
                "         (SELECT SUM(pieces) FROM order_claims WHERE order_id = :oid), 0)",
                {"oid": order_id, "worker": worker, "pieces": pieces},
            )
        except sqlite3.IntegrityError as err:
            raise AlreadyClaimed(f"{worker} already claimed order {order_id}") from err

        if cur.rowcount != 1:
            order = c.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise NoSuchOrder(f"no such order {order_id}")
            if order["status"] not in ("open", "claimed"):
                raise NotClaimable(f"order {order_id} is {order['status']}")
            raise InsufficientRemaining(
                f"order {order_id} does not have {pieces} unclaimed pieces remaining"
            )

        claim_id = cur.lastrowid
        c.execute("UPDATE orders SET status = 'claimed' WHERE id = ? AND status = 'open'",
                   (order_id,))
        return claim_id


def mark_fulfilled(order_id: int, worker: str, delivered_pieces: int, *,
                    conn: Optional[sqlite3.Connection] = None) -> str:
    """Record that `worker` actually delivered `delivered_pieces` pieces.

    `produced_pieces`/`status` are distinct from `order_claims` on purpose: a
    claimed order is NOT a delivered one. This bounds the delivery to the
    worker's OWN claimed pieces in one UPDATE ... WHERE (delivered + delta
    <= pieces), then recomputes the order's total from the SUM across every
    claim -- never from this one worker's report -- so no single worker's
    update can flip the order to `awaiting_verification` unless the order as
    a whole is actually fully produced. Returns the order's new status.
    """
    delivered_pieces = _positive_int(delivered_pieces, "delivered_pieces")
    worker = normalise_subject(worker)
    with db_in(conn) as c:
        cur = c.execute(
            "UPDATE order_claims SET delivered = delivered + :d "
            " WHERE order_id = :oid AND worker = :worker AND delivered + :d <= pieces",
            {"d": delivered_pieces, "oid": order_id, "worker": worker},
        )
        if cur.rowcount != 1:
            row = c.execute(
                "SELECT pieces, delivered FROM order_claims WHERE order_id = ? AND worker = ?",
                (order_id, worker),
            ).fetchone()
            if row is None:
                raise NoSuchClaim(f"{worker} has no claim on order {order_id}")
            raise OverDelivery(
                f"{worker} claimed {row['pieces']}, already delivered {row['delivered']}, "
                f"cannot add {delivered_pieces} more"
            )

        order = c.execute(
            "SELECT requested_pieces, status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None or order["status"] not in ("claimed", "awaiting_verification"):
            raise NotClaimable(f"order {order_id} is not in a deliverable state")

        total = c.execute(
            "SELECT COALESCE(SUM(delivered), 0) AS t FROM order_claims WHERE order_id = ?",
            (order_id,),
        ).fetchone()["t"]
        new_status = "awaiting_verification" if total >= order["requested_pieces"] else "claimed"
        c.execute(
            "UPDATE orders SET produced_pieces = :t, status = :st WHERE id = :oid",
            {"t": total, "st": new_status, "oid": order_id},
        )
        return new_status


def cancel(order_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Cancel an order. Only possible before delivery is verified -- an order
    already `awaiting_verification` or `fulfilled` has real work or a real
    payout riding on it and must not vanish silently."""
    with db_in(conn) as c:
        cur = c.execute(
            "UPDATE orders SET status = 'cancelled', closed_at = datetime('now') "
            " WHERE id = ? AND status IN ('open', 'claimed')",
            (order_id,),
        )
        if cur.rowcount != 1:
            raise NotClaimable(f"order {order_id} cannot be cancelled from its current state")


def approve(order_id: int, approver: str, *,
            conn: Optional[sqlite3.Connection] = None) -> dict[str, int]:
    """Verify and pay out a fully-produced order. Pays each claim via
    `money.transfer` from `treasury:shop`.

    Three mandatory guards, each with the bug it prevents:

    1. Self-approval refused -- anyone who claimed (and therefore, by the
       same row, delivered) this order cannot also sign off on paying it.
       Checked against `order_claims`, which is the one place "claimed or
       fulfilled" is recorded for a worker.
    2. A zero or missing snapshot price raises `ZeroPrice` before any
       transfer happens -- a corrupted price snapshot must be a loud
       failure, never a silent 0-coin transfer that *looks* like a
       successful payout in the ledger.
    3. Each claim's `paid_event`/`paid_coins` are written by an
       UPDATE ... WHERE paid_event IS NULL -- the SAME statement that
       decides "should this claim be paid" also claims the unique payout
       slot, so a second `approve()` call (this order re-approved, or two
       calls racing and serialized by BEGIN IMMEDIATE) can win the gate on
       at most one attempt per claim and therefore can pay it at most once.
    """
    approver = normalise_subject(approver)
    with db_in(conn) as c:
        order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise NoSuchOrder(f"no such order {order_id}")
        if order["status"] != "awaiting_verification":
            raise NotClaimable(
                f"order {order_id} is {order['status']}, not awaiting_verification"
            )

        worked = c.execute(
            "SELECT 1 FROM order_claims WHERE order_id = ? AND worker = ?",
            (order_id, approver),
        ).fetchone()
        if worked is not None:
            raise SelfApproval(
                f"{approver} claimed or fulfilled order {order_id}; cannot approve their own order"
            )

        price = order["price_coins"]
        unit_pieces = order["price_unit_pieces"]
        if not price or price <= 0:
            raise ZeroPrice(
                f"order {order_id} has snapshot price {price!r}; refusing to pay a zero-coin claim"
            )

        # ORDER BY claimed_at, id: split_charge's cumulative differencing
        # only sums correctly, and only reproduces the SAME per-claim amounts
        # on a retry, if every call sorts claims into the identical, stable
        # order. See core/pricing.py:split_charge for why this exists --
        # per-claim charge() lets colluding/sock-puppet claimants fragment
        # one order into many tiny claims and collect the rounding on each
        # one (a 64-piece order at 300/stack pays 300 as one claim but 320 as
        # sixty-four 1-piece claims).
        claims = c.execute(
            "SELECT * FROM order_claims WHERE order_id = ? ORDER BY claimed_at, id",
            (order_id,),
        ).fetchall()
        payouts = split_charge([cl["delivered"] for cl in claims], price, unit_pieces)

        paid_total = 0
        paid_claims = 0
        ops: list[dict] = []
        for cl, amount in zip(claims, payouts):
            if amount <= 0:
                continue                                      # nothing to pay for -- not an error
            event_id = money.new_event_id("payout")
            won = c.execute(
                "UPDATE order_claims SET paid_event = :evt, paid_coins = :amt "
                " WHERE id = :cid AND paid_event IS NULL",
                {"evt": event_id, "amt": amount, "cid": cl["id"]},
            )
            if won.rowcount != 1:
                continue                                       # a prior approve already paid this
            worker = normalise_subject(cl["worker"])
            money.transfer(
                "treasury:shop", worker, amount,
                service="shop", reason=f"order #{order_id} payout",
                ref_kind="order", ref_id=str(order_id), idem_key=event_id, conn=c,
            )
            paid_total += amount
            paid_claims += 1
            ops.append({
                "op": "transfer", "src": "treasury:shop", "dst": worker, "amount": amount,
                "reverse": {"op": "transfer", "src": worker, "dst": "treasury:shop",
                            "amount": amount},
            })

        c.execute(
            "UPDATE orders SET status = 'fulfilled', closed_at = datetime('now') WHERE id = ?",
            (order_id,),
        )

        audit.record(
            c, actor=approver, target=f"order:{order_id}", kind="order.approve",
            summary=(
                f"approved order {order_id}: paid {paid_total:,} {CURRENCY} "
                f"across {paid_claims} claim(s)"
            ),
            ops=ops, money_coins=paid_total, manual_coins=0,
        )
        return {"paid_coins": paid_total, "paid_claims": paid_claims}
