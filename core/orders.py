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
  successful payout (`ZeroPrice`, and `ZeroPayout` for an order whose whole
  payout telescopes to 0).
- every non-closed order has at least one reachable terminal transition:
  pay (`approve`, after `reprice` if the snapshot needs repairing) or void
  (`cancel`, allowed from `awaiting_verification`). Never neither.
- `order_claims.paid_event` (UNIQUE) is set inside the SAME UPDATE that
  decides to pay a claim, which is the actual double-pay guard; `approve()`
  merely respects it rather than re-implementing it.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from . import audit, loyalty, money, teams
from .db import db_in, get_config
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


class ZeroPayout(OrderError):
    """Real work was delivered but the WHOLE order's payout computes to 0.

    CONTRACT.md sec 8 rule 11: a missing or zero price is a LOUD failure at
    payout, never a silent zero-coin payment. `ZeroPrice` catches a zero
    snapshot price; this catches the other way of arriving at nothing -- a
    non-zero price so small, against so few pieces, that `split_charge`
    telescopes the entire order to 0. Paying nobody and then closing the
    order permanently is indistinguishable, in the ledger, from a successful
    payout; the worker's delivered labour simply vanishes. Raising leaves the
    order in `awaiting_verification`, where staff can `reprice()` it or
    `cancel()` it. A per-claim 0 inside a non-zero total is NOT this error --
    that is legitimate rounding and is skipped, not raised.
    """


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

#: What a worker is paid, as a percent of the shop's sell price for the same
#: quantity. The shop's margin is the remainder, and it has to be wide enough
#: to survive what gets added on TOP of a payout: a top-rank loyalty bonus
#: (12%) and a manager override (5%) compound onto the worker's figure, so at
#: 70% the treasury still keeps roughly 18 of every 100 it charges. Set this
#: above ~85 and a fully-loaded order costs more than the sale that funded it.
WORKER_PAYOUT_PCT = 70


#: The config key the live margin is stored under. `WORKER_PAYOUT_PCT` above
#: is the DEFAULT, used when nothing has been set; the stored value wins.
#: This is the shop's margin -- a business number the owner will want to move
#: without an edit and a redeploy, on a host with no shell where a redeploy
#: means a git push and a panel pull.
PAYOUT_PCT_KEY = "worker_payout_pct"


def payout_pct(*, conn: Optional[sqlite3.Connection] = None) -> int:
    """The margin currently in force, from `config`, falling back to the
    constant.

    Bounded to 1..100 on read rather than trusted: this decides what leaves
    the treasury on every approval, and a stored 0 would pay workers
    nothing while a stored 500 would pay five times the sale price. A bad
    value in a config row must degrade to the default, never to a payout.
    """
    # Best-effort by design. This is a PRICING helper: it is called from
    # inside open transactions, from tests before `init_db` has created the
    # config table, and from scripts with no database at all. None of those
    # should raise -- a margin that cannot be read is the default margin,
    # not a crash in the middle of opening somebody's order.
    try:
        raw = get_config(PAYOUT_PCT_KEY, conn=conn)
    except sqlite3.Error:
        return WORKER_PAYOUT_PCT
    if raw is None:
        return WORKER_PAYOUT_PCT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return WORKER_PAYOUT_PCT
    if not 1 <= value <= 100:
        return WORKER_PAYOUT_PCT
    return value


def worker_payout_for(price_coins: int, *, pct: int | None = None,
                       conn: Optional[sqlite3.Connection] = None) -> int:
    """The payout rate to snapshot onto an order priced at `price_coins`.

    Floor division, so rounding favours the treasury by at most one coin
    per unit rather than against it -- the opposite bias compounds into a
    real deficit across thousands of orders.

    NEVER returns 0 for a priced item. At 70%, anything priced 1 floors to
    0, and a zero payout rate is not "cheap work" -- `approve()` raises
    ZeroPrice on it, so the order can never be paid and delivered work sits
    stranded until staff notice and reprice it. Money is whole coins here,
    so below about 2 there is simply no margin to take: the shop pays the
    full 1 and takes nothing rather than breaking the order. A deliberate
    floor, not a rounding accident.
    """
    price = int(price_coins)
    if price <= 0:
        return 0
    # `conn` is passed DOWN, never re-opened: this is called from inside
    # create_order's and reprice's open transaction, and a fresh db()
    # there commits the caller's half-written work (core/db.py's own
    # warning, earned twice).
    rate = payout_pct(conn=conn) if pct is None else pct
    return max(1, (price * rate) // 100)


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

    TWO prices are snapshotted, not one. `price_coins` is what the shop
    SELLS at; `payout_coins` is what it PAYS a worker to produce the same
    quantity, and the gap between them is the shop's margin. They were the
    same number once, which meant every completed order paid out exactly
    what it charged -- and once a loyalty bonus landed on top, more:
    measured at 375 paid against a 320 sale. A shop that pays more for
    goods than it sells them for drains its own treasury on every order it
    completes, and no amount of funding fixes a per-unit loss.
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
        payout_coins = worker_payout_for(item["price_coins"], conn=c)
        cur = c.execute(
            "INSERT INTO orders (item_id, requested_pieces, price_coins, payout_coins, "
            "price_unit_pieces, stack_size, created_by, channel_id, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, requested_pieces, item["price_coins"], payout_coins,
             item["price_unit_pieces"], item["stack_size"], created_by,
             channel_id, message_id),
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


def cancel(order_id: int, *, actor: str = "system", reason: str | None = None,
           conn: Optional[sqlite3.Connection] = None) -> None:
    """Cancel an order. Allowed from 'open', 'claimed' AND
    'awaiting_verification'; refused from 'fulfilled' and 'cancelled'.

    WHY 'awaiting_verification' IS ALLOWED -- the order dead end. This used to
    stop at ('open', 'claimed') on the reasoning that a delivered order "has
    real work riding on it and must not vanish silently". The effect was the
    opposite of that intent: an order whose price snapshot is 0 (or whose
    whole payout rounds to 0) raises `ZeroPrice`/`ZeroPayout` from
    `approve()` forever, and with cancel refused as well the order had NO
    reachable terminal transition at all. The delivered labour was lost
    anyway, the approval queue filled with zombies staff could not clear, and
    the pieces stayed claimed so nobody else could take the order either.

    The invariant is: FOR EVERY NON-CLOSED ORDER THERE IS AT LEAST ONE
    REACHABLE TERMINAL TRANSITION -- pay (`approve`, after `reprice` if the
    snapshot needs repairing) or void (`cancel`). Never neither.

    Cancelling never moves money. Work that was delivered and never paid is
    recorded on the audit row as a `manual_coins` debt with one op per unpaid
    claim, in the SAME transaction as the status change, so a human can settle
    it deliberately instead of it disappearing without trace.
    """
    actor = normalise_subject(actor)
    with db_in(conn) as c:
        cur = c.execute(
            "UPDATE orders SET status = 'cancelled', closed_at = datetime('now') "
            " WHERE id = ? AND status IN ('open', 'claimed', 'awaiting_verification')",
            (order_id,),
        )
        if cur.rowcount != 1:
            row = c.execute("SELECT status FROM orders WHERE id = ?",
                            (order_id,)).fetchone()
            if row is None:
                raise NoSuchOrder(f"no such order {order_id}")
            raise NotClaimable(
                f"order {order_id} is {row['status']}; a closed order cannot be cancelled"
            )

        order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        claims = c.execute(
            "SELECT * FROM order_claims WHERE order_id = ? ORDER BY claimed_at, id",
            (order_id,),
        ).fetchall()

        # Same ordering and same splitter approve() would have used, so the
        # figure a human is asked to settle is the figure the order would have
        # paid. A zero/absent snapshot price simply owes 0 -- the pieces are
        # still listed, which is the point of the row.
        price = order["price_coins"]
        unit_pieces = order["price_unit_pieces"]
        if price and price > 0:
            owed = split_charge([cl["delivered"] for cl in claims], price, unit_pieces)
        else:
            owed = [0] * len(claims)

        ops: list[dict] = []
        manual_total = 0
        for cl, amount in zip(claims, owed):
            if cl["paid_event"] is not None or cl["delivered"] <= 0:
                continue
            worker = normalise_subject(cl["worker"])
            ops.append({
                "op": "manual",
                "worker": worker,
                "delivered_pieces": cl["delivered"],
                "amount": amount,
                "note": (f"delivered {cl['delivered']} piece(s) on cancelled order "
                         f"{order_id}, never paid -- settle by hand"),
            })
            manual_total += amount

        summary = f"cancelled order {order_id}"
        if reason:
            summary += f": {reason}"
        if ops:
            summary += (f" -- {len(ops)} unpaid delivered claim(s) owing "
                        f"{manual_total:,} {CURRENCY} by hand")
        audit.record(
            c, actor=actor, target=f"order:{order_id}", kind="order.cancel",
            summary=summary, ops=ops, money_coins=0, manual_coins=manual_total,
        )


def reprice(order_id: int, price_coins: int, price_unit_pieces: int | None = None,
            *, actor: str, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Repair an order's PRICE SNAPSHOT so a stuck order can actually be paid.

    The snapshot is deliberately immutable against the item's live columns
    (see `create_order`) -- but that also means a snapshot taken while the
    item was mispriced, or priced at 0, can never be corrected, and the
    delivered order can never be approved. This is the deliberate staff
    repair: it changes the price the order will pay, and NOTHING else. It
    never moves money, never touches claims, never closes the order.

    Refused once the order is 'fulfilled' or 'cancelled' -- a paid order is
    never repriced, because the money is already out the door. `price_coins`
    must be a POSITIVE int (repricing to 0 would re-create the dead end this
    function exists to clear); `price_unit_pieces` defaults to the order's
    current value, must be positive, and must not exceed the order's
    `stack_size` -- the same rule `catalog.add_item` and the schema CHECK
    enforce for items.
    """
    price_coins = _positive_int(price_coins, "price_coins")
    if price_unit_pieces is not None:
        price_unit_pieces = _positive_int(price_unit_pieces, "price_unit_pieces")
    actor = normalise_subject(actor)
    with db_in(conn) as c:
        order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise NoSuchOrder(f"no such order {order_id}")
        if order["status"] in ("fulfilled", "cancelled"):
            raise NotClaimable(
                f"order {order_id} is {order['status']}; a closed order is never repriced"
            )
        unit_pieces = (order["price_unit_pieces"] if price_unit_pieces is None
                       else price_unit_pieces)
        if unit_pieces > order["stack_size"]:
            raise ValueError("price_unit_pieces must not exceed stack_size")

        before_price = order["price_coins"]
        before_unit = order["price_unit_pieces"]
        cur = c.execute(
            # payout_coins moves WITH the price. Repricing exists so a
            # stuck order can finally be paid, and leaving the payout at
            # the old snapshot would defeat exactly that: staff would fix
            # the sell price, approve, and still pay the broken figure.
            "UPDATE orders SET price_coins = :p, payout_coins = :pay, "
            "                  price_unit_pieces = :u "
            " WHERE id = :oid AND status NOT IN ('fulfilled', 'cancelled')",
            {"p": price_coins, "pay": worker_payout_for(price_coins, conn=c),
             "u": unit_pieces, "oid": order_id},
        )
        if cur.rowcount != 1:                       # closed underneath us
            raise NotClaimable(
                f"order {order_id} was closed before the reprice could be applied"
            )

        audit.record(
            c, actor=actor, target=f"order:{order_id}", kind="order.reprice",
            summary=(
                f"repriced order {order_id} from {before_price:,} {CURRENCY} "
                f"per {before_unit} piece(s) to {price_coins:,} {CURRENCY} "
                f"per {unit_pieces} piece(s)"
            ),
            ops=[{
                "op": "reprice",
                "before": {"price_coins": before_price, "price_unit_pieces": before_unit},
                "after": {"price_coins": price_coins, "price_unit_pieces": unit_pieces},
                "reverse": {"op": "reprice", "price_coins": before_price,
                            "price_unit_pieces": before_unit},
            }],
            money_coins=0, manual_coins=0,
        )
        row = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return _row(row)


def _payout_plan(c: sqlite3.Connection, claims, payouts) -> dict[str, Any]:
    """What approving this order will actually cost the treasury, computed
    once and used by BOTH `preview_approval` and `approve`.

    It exists because those two drifted: `approve` added each worker's
    loyalty bonus on top of the priced amount while `preview_approval`
    returned the bare `sum(payouts)`, so the figure staff confirmed was
    short by every bonus in the order -- 320 against a real 375 in the case
    that found it. A preview whose docstring promises the exact figure has
    to be computed by the same code that pays it, not by a second
    implementation that agrees only until one of them changes.

    Read-only: it decides amounts, never writes a row or moves a coin.
    """
    per_claim: list[dict[str, Any]] = []
    overrides: dict[str, int] = {}
    for cl, amount in zip(claims, payouts):
        if amount <= 0:
            continue                                      # nothing to pay for -- not an error
        worker = normalise_subject(cl["worker"])
        # The worker's CURRENT tier: this order's own payout must not
        # retroactively rank them up before pricing itself.
        bonus_pct = loyalty.payout_bonus_pct(worker, conn=c)
        bonus = (amount * bonus_pct) // 100
        total_amount = amount + bonus
        per_claim.append({
            "claim_id": cl["id"], "worker": worker,
            "delivered_pieces": cl["delivered"], "amount": amount,
            "bonus": bonus, "bonus_pct": bonus_pct, "total": total_amount,
        })
        manager = teams.manager_of(worker, conn=c)
        if manager:
            cut = (total_amount * teams.MANAGER_OVERRIDE_PCT) // 100
            if cut > 0:
                overrides[manager] = overrides.get(manager, 0) + cut

    payout_total = sum(row["total"] for row in per_claim)
    override_total = sum(overrides.values())
    return {
        "per_claim": per_claim,
        "overrides": overrides,
        "payout_total": payout_total,
        "override_total": override_total,
        "total_coins": payout_total + override_total,
    }


def preview_approval(order_id: int, approver: str, *,
                      conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Read-only dry run of `approve()`'s payout maths, for showing staff the
    exact figure they are about to confirm -- never on a screen without
    computing it first (CONTRACT.md sec 8 rule "the unit is the content").

    Runs every guard `approve()` runs (self-approval, ZeroPrice, ZeroPayout)
    and raises the SAME exceptions, so a preview that comes back clean is a
    reliable promise: `approve()` on the same, unchanged order pays exactly
    `total_coins` split exactly as `per_claim` shows. It computes with
    `split_charge` over the SAME `ORDER BY claimed_at, id` ordering `approve()`
    uses, so it is not a separate estimate that could drift from the real
    payout -- it is the real payout, just not written yet.
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

        # The PAYOUT rate, not the sell price: `payout_coins` is what this
        # order promised a worker when it was opened.
        price = order["payout_coins"] or order["price_coins"]
        unit_pieces = order["price_unit_pieces"]
        if not price or price <= 0:
            raise ZeroPrice(
                f"order {order_id} has snapshot price {price!r}; refusing to pay a zero-coin claim"
            )

        claims = c.execute(
            "SELECT * FROM order_claims WHERE order_id = ? ORDER BY claimed_at, id",
            (order_id,),
        ).fetchall()
        payouts = split_charge([cl["delivered"] for cl in claims], price, unit_pieces)

        delivered_total = sum(cl["delivered"] for cl in claims)
        if delivered_total > 0 and sum(payouts) <= 0:
            raise ZeroPayout(
                f"order {order_id} delivered {delivered_total} piece(s) but its whole "
                f"payout computes to 0 at {price:,} {CURRENCY} per {unit_pieces} "
                f"piece(s); refusing to pay zero and close -- reprice or cancel it"
            )

        plan = _payout_plan(c, claims, payouts)
        # `total_coins` is what LEAVES THE TREASURY: every claim's priced
        # amount, plus each worker's loyalty bonus, plus any manager
        # override the order triggers. Staff confirm this number, so it is
        # the whole cost or it is a lie.
        return {
            "order_id": order_id,
            "total_coins": plan["total_coins"],
            "payout_coins": plan["payout_total"],
            "override_coins": plan["override_total"],
            "overrides": dict(plan["overrides"]),
            "paid_claims": len(plan["per_claim"]),
            "per_claim": plan["per_claim"],
        }


def approve(order_id: int, approver: str, *,
            conn: Optional[sqlite3.Connection] = None) -> dict[str, int]:
    """Verify and pay out a fully-produced order. Pays each claim via
    `money.transfer` from `treasury:shop`.

    Three mandatory guards, each with the bug it prevents:

    1. Self-approval refused -- anyone who claimed (and therefore, by the
       same row, delivered) this order cannot also sign off on paying it.
       Checked against `order_claims`, which is the one place "claimed or
       fulfilled" is recorded for a worker.
    2. A zero or missing snapshot price raises `ZeroPrice`, and an order
       whose ENTIRE payout telescopes to 0 raises `ZeroPayout` -- both
       before any transfer happens and before the order is closed. A
       payout of nothing must be a loud failure, never a silent 0-coin
       transfer that *looks* like a successful payout in the ledger. The
       order stays `awaiting_verification` either way, so staff can
       `reprice()` or `cancel()` it: a delivered order ALWAYS has an exit.
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

        # The PAYOUT rate, not the sell price -- same source as
        # preview_approval, so the two cannot disagree. `or price_coins`
        # covers a row written before payout_coins existed whose backfill
        # has not run, which pays the old way rather than paying nothing.
        price = order["payout_coins"] or order["price_coins"]
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

        # CONTRACT.md sec 8 rule 11, the other half of the ZeroPrice guard. A
        # non-zero price can still telescope the WHOLE order to 0 (one piece
        # at 1 coin per 64 pieces: charge(1, 1, 64) == 0). The old loop then
        # paid nobody, reported paid_coins=0, and closed the order
        # 'fulfilled' -- a silent zero-coin payment that permanently buried
        # real delivered work. Raise BEFORE any claim is marked paid and
        # BEFORE the order is closed, leaving it in awaiting_verification so
        # staff can reprice() or cancel() it. A per-claim 0 inside a non-zero
        # total is legitimate rounding and is skipped below, not raised.
        delivered_total = sum(cl["delivered"] for cl in claims)
        if delivered_total > 0 and sum(payouts) <= 0:
            raise ZeroPayout(
                f"order {order_id} delivered {delivered_total} piece(s) but its whole "
                f"payout computes to 0 at {price:,} {CURRENCY} per {unit_pieces} "
                f"piece(s); refusing to pay zero and close -- reprice or cancel it"
            )

        # The SAME plan `preview_approval` showed staff -- amounts, loyalty
        # bonuses and manager overrides all decided in one place, so the
        # figure confirmed and the figure paid cannot drift apart.
        plan = _payout_plan(c, claims, payouts)

        paid_total = 0
        paid_claims = 0
        ops: list[dict] = []
        overrides: dict[str, int] = {}
        for row in plan["per_claim"]:
            worker, total_amount = row["worker"], row["total"]
            bonus, bonus_pct = row["bonus"], row["bonus_pct"]

            # paid_coins stores the TOTAL actually paid, bonus included:
            # core/loyalty.py sums it to compute future points, and a bonus
            # the worker was really paid counts toward their own rank the
            # same as the base amount does.
            event_id = money.new_event_id("payout")
            won = c.execute(
                "UPDATE order_claims SET paid_event = :evt, paid_coins = :amt "
                " WHERE id = :cid AND paid_event IS NULL",
                {"evt": event_id, "amt": total_amount, "cid": row["claim_id"]},
            )
            if won.rowcount != 1:
                continue                                       # a prior approve already paid this
            money.transfer(
                "treasury:shop", worker, total_amount,
                service="shop", reason=(
                    f"order #{order_id} payout"
                    + (f" (includes {bonus:,} loyalty bonus at {bonus_pct}%)" if bonus else "")
                ),
                ref_kind="order", ref_id=str(order_id), idem_key=event_id, conn=c,
            )
            paid_total += total_amount
            paid_claims += 1
            ops.append({
                "op": "transfer", "src": "treasury:shop", "dst": worker, "amount": total_amount,
                "reverse": {"op": "transfer", "src": worker, "dst": "treasury:shop",
                            "amount": total_amount},
            })

            # Accrued only inside the won-the-gate branch, so a re-approve
            # that skips an already-paid claim skips its override too --
            # otherwise the second approval would pay the manager again on
            # work already settled.
            manager = teams.manager_of(worker, conn=c)
            if manager:
                cut = (total_amount * teams.MANAGER_OVERRIDE_PCT) // 100
                if cut > 0:
                    overrides[manager] = overrides.get(manager, 0) + cut

        # ---------------------------------------------------------- manager overrides
        # THE COMPANY PAYS THIS. It is a fresh transfer out of
        # `treasury:shop`, never a deduction from what the worker earned --
        # the worker's payout above is already final and untouched. Carried
        # over from AbexTech, where this rule is recorded in the code as
        # the owner's own ruling after an audit: an override taken off the
        # worker silently pays them less than the price they claimed
        # against, and one minted from nothing inflates the money supply
        # per order.
        #
        # In the SAME transaction as the payouts, because two commits have
        # no safe ordering: a crash between them either mints the override
        # (credit first) or destroys it (debit first).
        #
        # An override is never allowed to cost the WORKER their payment. If
        # the treasury cannot cover it, the shortfall is skipped and
        # recorded, not raised -- `money.transfer` would otherwise roll the
        # whole approval back and the delivered work would go unpaid over a
        # commission. Availability is read here rather than assumed, inside
        # the same BEGIN IMMEDIATE, so nothing can move underneath it.
        override_total = 0
        override_unpaid = 0
        for manager, cut in sorted(overrides.items()):
            available = money.balance("treasury:shop", conn=c).available
            payable = min(cut, max(available, 0))
            if payable <= 0:
                override_unpaid += cut
                continue
            if payable < cut:
                override_unpaid += cut - payable
            event_id = money.new_event_id("override")
            money.transfer(
                "treasury:shop", manager, payable,
                service="shop",
                reason=(f"order #{order_id} manager override at "
                        f"{teams.MANAGER_OVERRIDE_PCT}% on their team's payouts"),
                ref_kind="order", ref_id=str(order_id), idem_key=event_id, conn=c,
            )
            # Recorded as a row, in this same transaction, so "what has
            # managing earned" is a join rather than a scan of free-text
            # ledger reasons.
            c.execute(
                "INSERT INTO team_overrides (order_id, manager, coins, paid_event) "
                "VALUES (?, ?, ?, ?)",
                (order_id, manager, payable, event_id),
            )
            override_total += payable
            ops.append({
                "op": "transfer", "src": "treasury:shop", "dst": manager, "amount": payable,
                "reverse": {"op": "transfer", "src": manager, "dst": "treasury:shop",
                            "amount": payable},
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
                + (f", plus {override_total:,} {CURRENCY} in manager override(s)"
                   if override_total else "")
                + (f" ({override_unpaid:,} {CURRENCY} of override unfunded)"
                   if override_unpaid else "")
            ),
            ops=ops, money_coins=paid_total + override_total, manual_coins=0,
        )
        return {
            "paid_coins": paid_total,
            "paid_claims": paid_claims,
            "override_coins": override_total,
            "override_unpaid": override_unpaid,
        }
