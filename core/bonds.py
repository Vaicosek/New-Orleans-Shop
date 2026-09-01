"""core/bonds.py -- treasury-issued, fixed-rate bonds.

Simple version: `treasury:shop` issues a bond series (a unit price, a
total unit count, a coupon rate in basis points, a coupon interval, a
term) and players buy units outright -- a plain `money.transfer` from
buyer to treasury, no escrow, this is a sale, not a bid. There is no
company, no stock market, no item collateral, and no default handling:
this is deliberately NOT AbexTech's item-collateralized corporate debt
(see CONTRACT.md section 11b) -- it is the treasury's own IOU, and the
treasury either has the coins to pay a coupon/maturity or it doesn't
(deficit_floor 0, same as every other treasury payout in this codebase).

Lifecycle: `issue` (staff) -> `buy` (repeatable, any open holder) ->
periodic `pay_coupon` while `next_coupon_at < matures_at`, then `mature`
once `matures_at` passes -- both driven off `sweep_expired`, called by a
loop in bot/cogs/admin.py alongside the auction/land sweeps. `void`
(staff-only, pre-maturity) refunds every current holder's principal (not
already-paid coupons) and cancels the series -- the escape hatch for a
bond issued by mistake.

`pay_coupon` and `mature` are each a single all-or-nothing transaction: if
the treasury cannot fund every holder's share, the WHOLE period rolls
back, including the `next_coupon_at` advance -- a period that could not be
fully paid is never marked paid, so the next sweep just retries it once
the treasury is funded. This is what makes the sweep loop safe to run on
whatever cadence it likes: a bond either fully settles a due period, or
nothing about it changes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import audit, money
from .db import db_in

SERVICE = "shop"
TREASURY = money.SERVICE_TREASURY[SERVICE]  # "treasury:shop"


class BondError(RuntimeError):
    """Base class. A refusal, never a partial apply."""


class UnknownBond(BondError): pass
class BondNotOpen(BondError): pass
class NotEnoughUnits(BondError): pass
class NothingDue(BondError): pass
class AlreadySettled(BondError): pass


class OrdersBlocked(BondError):
    """`subject` has the `orders_blocked` wallet flag. Buying a bond is
    commerce, same reasoning as auctions/land's OrdersBlocked."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _bond(c: sqlite3.Connection, bond_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM bonds WHERE id = ?", (bond_id,)).fetchone()
    if row is None:
        raise UnknownBond(bond_id)
    return row


def _holdings(c: sqlite3.Connection, bond_id: int) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT subject, units FROM bond_holdings WHERE bond_id = ?", (bond_id,)
    ).fetchall()


# ------------------------------------------------------------------ lifecycle

def issue(name: str, unit_price: int, units_total: int, coupon_bps: int,
          coupon_interval_days: int, term_days: int, *, created_by: str,
          conn: Optional[sqlite3.Connection] = None) -> int:
    if not name or not name.strip():
        raise BondError("name is required")
    if unit_price <= 0:
        raise BondError("unit_price must be positive")
    if units_total <= 0:
        raise BondError("units_total must be positive")
    if coupon_bps < 0:
        raise BondError("coupon_bps cannot be negative")
    if coupon_interval_days <= 0:
        raise BondError("coupon_interval_days must be positive")
    if term_days <= 0:
        raise BondError("term_days must be positive")
    with db_in(conn) as c:
        now = datetime.now(timezone.utc)
        matures_at = (now + timedelta(days=term_days)).strftime("%Y-%m-%d %H:%M:%S")
        next_coupon_at = (now + timedelta(days=coupon_interval_days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = c.execute(
            "INSERT INTO bonds (name, unit_price, units_total, coupon_bps, "
            "coupon_interval_days, term_days, created_by, matures_at, next_coupon_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), unit_price, units_total, coupon_bps, coupon_interval_days,
             term_days, created_by, matures_at, next_coupon_at),
        )
        return cur.lastrowid


def buy(bond_id: int, subject: str, units: int, *,
        conn: Optional[sqlite3.Connection] = None) -> dict:
    """Buy `units` of `bond_id` outright. A straight sale, not a bid: the
    cost moves buyer -> treasury in the SAME transaction that claims the
    units, via a compare-and-swap on `units_sold` (`WHERE units_sold + ? <=
    units_total`) so two concurrent buyers can never oversell the series."""
    if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
        raise BondError("units must be a positive int")
    with db_in(conn) as c:
        bond = _bond(c, bond_id)
        if bond["status"] != "open":
            raise BondNotOpen(f"bond {bond_id} is {bond['status']}, not open")
        if bond["matures_at"] <= _now():
            raise BondNotOpen(f"bond {bond_id} has already reached maturity")

        if "orders_blocked" in money.flags(subject, conn=c):
            raise OrdersBlocked(f"{subject} has orders blocked")

        cur = c.execute(
            "UPDATE bonds SET units_sold = units_sold + ? "
            "WHERE id = ? AND units_sold + ? <= units_total",
            (units, bond_id, units),
        )
        if cur.rowcount != 1:
            remaining = bond["units_total"] - bond["units_sold"]
            raise NotEnoughUnits(f"only {remaining} unit(s) left, cannot sell {units}")

        cost = bond["unit_price"] * units
        money.transfer(
            subject, TREASURY, cost, service=SERVICE,
            reason=f"bought {units} unit(s) of bond {bond_id} ({bond['name']})",
            ref_kind="bond", ref_id=str(bond_id), conn=c,
        )
        c.execute(
            "INSERT INTO bond_holdings (bond_id, subject, units) VALUES (?, ?, ?) "
            "ON CONFLICT(bond_id, subject) DO UPDATE SET units = units + excluded.units",
            (bond_id, subject, units),
        )
        return {"bond_id": bond_id, "units": units, "cost": cost}


def set_message(bond_id: int, channel_id: str, message_id: str, *,
                 conn: Optional[sqlite3.Connection] = None) -> None:
    with db_in(conn) as c:
        c.execute("UPDATE bonds SET channel_id = ?, message_id = ? WHERE id = ?",
                  (str(channel_id), str(message_id), int(bond_id)))


def pay_coupon(bond_id: int, *, actor: str = "system:sweep",
               conn: Optional[sqlite3.Connection] = None) -> dict:
    """Pay the one currently-due coupon period to every holder, proportional
    to units held. All-or-nothing: if the treasury cannot fund every
    holder's share, `money.transfer` raises and the whole transaction --
    including the `next_coupon_at` advance below -- rolls back."""
    with db_in(conn) as c:
        bond = _bond(c, bond_id)
        if bond["status"] != "open":
            raise BondNotOpen(f"bond {bond_id} is {bond['status']}, not open")
        if bond["next_coupon_at"] > _now():
            raise NothingDue(f"bond {bond_id}'s next coupon isn't due yet")
        if bond["next_coupon_at"] >= bond["matures_at"]:
            raise NothingDue(f"bond {bond_id} is at or past maturity; call mature() instead")

        period_ts = bond["next_coupon_at"]
        new_next = (
            datetime.strptime(period_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            + timedelta(days=bond["coupon_interval_days"])
        ).strftime("%Y-%m-%d %H:%M:%S")

        # Compare-and-swap: only one caller can claim this exact period.
        cur = c.execute(
            "UPDATE bonds SET next_coupon_at = ? WHERE id = ? AND next_coupon_at = ?",
            (new_next, bond_id, period_ts),
        )
        if cur.rowcount != 1:
            raise AlreadySettled(f"bond {bond_id}'s {period_ts} coupon was already claimed")

        money.ensure_wallet(TREASURY, conn=c)
        total_paid = 0
        ops: list[dict] = []
        for h in _holdings(c, bond_id):
            amount = (bond["unit_price"] * h["units"] * bond["coupon_bps"]) // 10_000
            if amount <= 0:
                continue                                      # rounds to nothing -- not an error
            idem_key = f"bond.coupon:{bond_id}:{period_ts}:{h['subject']}"
            money.transfer(
                TREASURY, h["subject"], amount, service=SERVICE,
                reason=f"bond {bond_id} ({bond['name']}) coupon", ref_kind="bond",
                ref_id=str(bond_id), idem_key=idem_key, conn=c,
            )
            total_paid += amount
            ops.append({
                "op": "transfer", "src": TREASURY, "dst": h["subject"], "amount": amount,
                "reverse": {"op": "transfer", "src": h["subject"], "dst": TREASURY,
                            "amount": amount},
            })

        audit.record(
            c, actor=actor, target=f"bond:{bond_id}", kind="bond.coupon",
            summary=f"bond {bond_id} ({bond['name']}) paid its {period_ts} coupon: "
                    f"{total_paid:,} total across {len(ops)} holder(s)",
            ops=ops, money_coins=total_paid, manual_coins=0,
            action_key=f"audit:bond.coupon:{bond_id}:{period_ts}",
        )
        return {"bond_id": bond_id, "period": period_ts, "paid": total_paid}


def mature(bond_id: int, *, actor: str = "system:sweep",
           conn: Optional[sqlite3.Connection] = None) -> dict:
    """Repay principal to every holder and close the series. Same
    all-or-nothing shape as `pay_coupon`: a treasury short of funds rolls
    the whole thing back, including the status flip, so the next sweep
    just retries it."""
    with db_in(conn) as c:
        bond = _bond(c, bond_id)
        if bond["status"] != "open":
            raise BondNotOpen(f"bond {bond_id} is {bond['status']}, not open")
        if bond["matures_at"] > _now():
            raise NothingDue(f"bond {bond_id} has not reached maturity yet")

        cur = c.execute(
            "UPDATE bonds SET status = 'matured' WHERE id = ? AND status = 'open'",
            (bond_id,),
        )
        if cur.rowcount != 1:
            raise AlreadySettled(f"bond {bond_id} was matured concurrently")

        money.ensure_wallet(TREASURY, conn=c)
        total_paid = 0
        ops: list[dict] = []
        for h in _holdings(c, bond_id):
            principal = bond["unit_price"] * h["units"]
            idem_key = f"bond.principal:{bond_id}:{h['subject']}"
            money.transfer(
                TREASURY, h["subject"], principal, service=SERVICE,
                reason=f"bond {bond_id} ({bond['name']}) matured: principal repaid",
                ref_kind="bond", ref_id=str(bond_id), idem_key=idem_key, conn=c,
            )
            total_paid += principal
            ops.append({
                "op": "transfer", "src": TREASURY, "dst": h["subject"], "amount": principal,
                "reverse": {"op": "transfer", "src": h["subject"], "dst": TREASURY,
                            "amount": principal},
            })

        audit.record(
            c, actor=actor, target=f"bond:{bond_id}", kind="bond.mature",
            summary=f"bond {bond_id} ({bond['name']}) matured: {total_paid:,} principal "
                    f"repaid across {len(ops)} holder(s)",
            ops=ops, money_coins=total_paid, manual_coins=0,
            action_key=f"audit:bond.mature:{bond_id}",
        )
        return {"bond_id": bond_id, "principal_paid": total_paid}


def void(bond_id: int, *, actor: str = "unknown",
         conn: Optional[sqlite3.Connection] = None) -> bool:
    """Cancel a bond before maturity. Refunds every current holder's
    PRINCIPAL (not coupons already paid, which nobody has to give back) --
    the escape hatch for a series issued by mistake. Same idempotent shape
    as auctions'/land's `void`: a second void on an already-voided or
    already-matured bond is refused."""
    with db_in(conn) as c:
        bond = _bond(c, bond_id)
        if bond["status"] == "matured":
            raise AlreadySettled(f"bond {bond_id} already matured; cannot void")
        if bond["status"] == "voided":
            return False

        cur = c.execute(
            "UPDATE bonds SET status = 'voided' WHERE id = ? AND status = 'open'",
            (bond_id,),
        )
        if cur.rowcount != 1:
            raise AlreadySettled(f"bond {bond_id} was settled concurrently")

        total_refunded = 0
        ops: list[dict] = []
        for h in _holdings(c, bond_id):
            refund = bond["unit_price"] * h["units"]
            idem_key = f"bond.void:{bond_id}:{h['subject']}"
            money.transfer(
                TREASURY, h["subject"], refund, service=SERVICE,
                reason=f"bond {bond_id} ({bond['name']}) voided: principal refunded",
                ref_kind="bond", ref_id=str(bond_id), idem_key=idem_key, conn=c,
            )
            total_refunded += refund
            ops.append({
                "op": "transfer", "src": TREASURY, "dst": h["subject"], "amount": refund,
                "reverse": {"op": "transfer", "src": h["subject"], "dst": TREASURY,
                            "amount": refund},
            })

        audit.record(
            c, actor=actor, target=f"bond:{bond_id}", kind="bond.void",
            summary=f"voided bond {bond_id} ({bond['name']}): refunded {total_refunded:,} "
                    f"across {len(ops)} holder(s)",
            ops=ops, money_coins=0, manual_coins=0,
        )
    return True


def sweep_expired(*, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Pay every due coupon and mature every bond past its term. Same
    one-bad-row-never-blocks-the-rest shape as auctions'/land's
    `sweep_expired`, and the same reason a period only ever advances one
    tick per sweep call: if the loop was down over several coupon dates,
    each catches up on a LATER sweep tick rather than all at once here --
    simple, and safe, at the cost of a backlog draining gradually instead
    of instantly. Called on an interval by bot/cogs/admin.py."""
    with db_in(conn) as c:
        due_coupons = c.execute(
            "SELECT id FROM bonds WHERE status = 'open' AND next_coupon_at <= ? "
            "AND next_coupon_at < matures_at",
            (_now(),),
        ).fetchall()
        due_maturities = c.execute(
            "SELECT id FROM bonds WHERE status = 'open' AND matures_at <= ?", (_now(),)
        ).fetchall()

    # Catches money.MoneyError too, not just BondError: unlike auctions/land
    # (where settlement captures an already-placed hold), a coupon or a
    # maturity payout is a FRESH treasury transfer with nothing pre-escrowed
    # -- an underfunded treasury is a real, expected failure mode here, and
    # one bond the treasury can't currently afford must never block every
    # other bond's sweep.
    coupons_paid: list[int] = []
    for row in due_coupons:
        try:
            pay_coupon(row["id"])
        except (BondError, money.MoneyError):
            continue
        coupons_paid.append(row["id"])

    matured: list[int] = []
    for row in due_maturities:
        try:
            mature(row["id"])
        except (BondError, money.MoneyError):
            continue
        matured.append(row["id"])

    return {"coupons_paid": coupons_paid, "matured": matured}
