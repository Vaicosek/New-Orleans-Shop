"""Read-only listing queries for the Discord panels.

`core/orders.py` and `core/predictions.py` expose lifecycle operations, not
"list everything open" -- there was never a reason for the domain layer to
carry a display query. Rather than invent that API inside `core/` (which the
brief says not to touch or extend beyond `core/config.py`), these are plain
SELECTs against the same tables, kept here, read-only, with no business
logic: no money moves, no state transitions, nothing a domain module would
need to own. Every write still goes through `core.orders` / `core.money` /
`core.predictions` / `core.games`.
"""
from __future__ import annotations

from typing import Any, Optional

from core.db import db_in


def list_orders(statuses: tuple[str, ...], *, worker: str | None = None,
                 limit: int = 25) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = list(statuses)
    join = ""
    where_worker = ""
    if worker is not None:
        join = "JOIN order_claims oc ON oc.order_id = o.id"
        where_worker = "AND oc.worker = ?"
        params.append(worker)
    params.append(limit)
    with db_in() as c:
        rows = c.execute(
            f"SELECT DISTINCT o.id, o.item_id, i.name AS item_name, o.requested_pieces, "
            f"       o.produced_pieces, o.status, o.price_coins, o.price_unit_pieces, o.stack_size, "
            f"       o.created_at "
            f"  FROM orders o "
            f"  JOIN items i ON i.id = o.item_id "
            f"  {join} "
            f" WHERE o.status IN ({placeholders}) {where_worker} "
            f" ORDER BY o.created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_order_detail(order_id: int) -> Optional[dict[str, Any]]:
    with db_in() as c:
        row = c.execute(
            "SELECT o.*, i.name AS item_name FROM orders o "
            "JOIN items i ON i.id = o.item_id WHERE o.id = ?",
            (order_id,),
        ).fetchone()
    return dict(row) if row else None


def list_open_markets(*, include_closed: bool = False, limit: int = 25) -> list[dict[str, Any]]:
    statuses = ("open", "closed") if include_closed else ("open",)
    placeholders = ",".join("?" for _ in statuses)
    with db_in() as c:
        rows = c.execute(
            f"SELECT * FROM pred_markets WHERE status IN ({placeholders}) "
            f" ORDER BY created_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        markets = []
        for r in rows:
            d = dict(r)
            outs = c.execute(
                "SELECT id, label FROM pred_outcomes WHERE market_id = ? ORDER BY id", (d["id"],)
            ).fetchall()
            # (id, label) pairs, never bare labels -- a picker built on this
            # keys its Select values on `id`, not on outcome text, so a long
            # or unusual outcome name can never break the component (see
            # bot/views/pickers.py).
            d["outcomes"] = [{"id": o["id"], "label": o["label"]} for o in outs]
            pool = c.execute(
                "SELECT COALESCE(SUM(amount), 0) AS n FROM pred_stakes WHERE market_id = ?",
                (d["id"],),
            ).fetchone()["n"]
            d["pool"] = pool
            markets.append(d)
    return markets


def get_market_detail(market_id: int) -> Optional[dict[str, Any]]:
    markets = [m for m in list_open_markets(include_closed=True, limit=1000) if m["id"] == market_id]
    if markets:
        return markets[0]
    with db_in() as c:
        row = c.execute("SELECT * FROM pred_markets WHERE id = ?", (market_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        outs = c.execute(
            "SELECT id, label FROM pred_outcomes WHERE market_id = ? ORDER BY id", (market_id,)
        ).fetchall()
        d["outcomes"] = [{"id": o["id"], "label": o["label"]} for o in outs]
        return d


def list_user_stakes(subject: str, *, limit: int = 25) -> list[dict[str, Any]]:
    with db_in() as c:
        rows = c.execute(
            "SELECT ps.*, pm.question, po.label AS outcome_label, pm.status AS market_status "
            "  FROM pred_stakes ps "
            "  JOIN pred_markets pm ON pm.id = ps.market_id "
            "  JOIN pred_outcomes po ON po.id = ps.outcome_id "
            " WHERE ps.subject = ? ORDER BY ps.placed_at DESC LIMIT ?",
            (subject, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_recent_rounds(*, game: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    where = "WHERE state = 'settled'"
    params: list[Any] = []
    if game is not None:
        where += " AND game = ?"
        params.append(game)
    params.append(limit)
    with db_in() as c:
        rows = c.execute(
            f"SELECT id, game, client_seed, nonce, outcome_json, settled_at "
            f"  FROM game_rounds {where} ORDER BY settled_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def list_user_bets(subject: str, *, limit: int = 10) -> list[dict[str, Any]]:
    with db_in() as c:
        rows = c.execute(
            "SELECT gb.*, gr.game FROM game_bets gb "
            "  JOIN game_rounds gr ON gr.id = gb.round_id "
            " WHERE gb.subject = ? ORDER BY gb.placed_at DESC LIMIT ?",
            (subject, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_auction_detail(auction_id: int) -> Optional[dict[str, Any]]:
    """One auction plus its item name and current leading bid (if any). The
    leader is read the same way core/auctions.py's `_leading_bid` reads it --
    the single `status = 'active'` row -- so the card can never show a
    different leader than the one settle() would actually pay."""
    with db_in() as c:
        row = c.execute(
            "SELECT a.*, i.name AS item_name FROM auctions a "
            "JOIN items i ON i.id = a.item_id WHERE a.id = ?",
            (auction_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        leader = c.execute(
            "SELECT subject, amount FROM auction_bids "
            "WHERE auction_id = ? AND status = 'active' "
            "ORDER BY amount DESC, id ASC LIMIT 1",
            (auction_id,),
        ).fetchone()
        d["leader_subject"] = leader["subject"] if leader else None
        d["leader_amount"] = leader["amount"] if leader else None
        d["bid_count"] = c.execute(
            "SELECT COUNT(*) AS n FROM auction_bids WHERE auction_id = ?", (auction_id,)
        ).fetchone()["n"]
    return d


def list_open_auctions(*, include_closed: bool = True, limit: int = 25) -> list[dict[str, Any]]:
    """Auctions still reachable for staff action (voiding): open, and closed-
    but-not-yet-swept. Settled and voided auctions are excluded -- there is
    nothing left to do to them from here."""
    statuses = ("open", "closed") if include_closed else ("open",)
    placeholders = ",".join("?" for _ in statuses)
    with db_in() as c:
        rows = c.execute(
            f"SELECT a.id, a.item_id, i.name AS item_name FROM auctions a "
            f"JOIN items i ON i.id = a.item_id "
            f"WHERE a.status IN ({placeholders}) ORDER BY a.created_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_land_detail(land_id: int) -> Optional[dict[str, Any]]:
    """One land listing plus its current leading bid (if any). Same shape
    as `get_auction_detail` -- the leader is read the same way
    core/land.py's `_leading_bid` reads it, so the card can never show a
    different leader than the one settle() would actually pay."""
    with db_in() as c:
        row = c.execute("SELECT * FROM land_listings WHERE id = ?", (land_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        leader = c.execute(
            "SELECT subject, amount FROM land_bids "
            "WHERE land_id = ? AND status = 'active' "
            "ORDER BY amount DESC, id ASC LIMIT 1",
            (land_id,),
        ).fetchone()
        d["leader_subject"] = leader["subject"] if leader else None
        d["leader_amount"] = leader["amount"] if leader else None
        d["bid_count"] = c.execute(
            "SELECT COUNT(*) AS n FROM land_bids WHERE land_id = ?", (land_id,)
        ).fetchone()["n"]
    return d


def list_open_land(*, include_closed: bool = True, limit: int = 25) -> list[dict[str, Any]]:
    """Listings still reachable for staff action (voiding): open, and
    closed-but-not-yet-swept. Same exclusion as `list_open_auctions`."""
    statuses = ("open", "closed") if include_closed else ("open",)
    placeholders = ",".join("?" for _ in statuses)
    with db_in() as c:
        rows = c.execute(
            f"SELECT id, name FROM land_listings "
            f"WHERE status IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_bond_detail(bond_id: int) -> Optional[dict[str, Any]]:
    """One bond plus its holder count -- enough for the public card, which
    shows units sold/remaining, next coupon date and maturity from the
    bond row itself."""
    with db_in() as c:
        row = c.execute("SELECT * FROM bonds WHERE id = ?", (bond_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["holder_count"] = c.execute(
            "SELECT COUNT(*) AS n FROM bond_holdings WHERE bond_id = ?", (bond_id,)
        ).fetchone()["n"]
    return d


def list_open_bonds(*, limit: int = 25) -> list[dict[str, Any]]:
    with db_in() as c:
        rows = c.execute(
            "SELECT id, name FROM bonds WHERE status = 'open' ORDER BY issued_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_loan_detail(loan_id: int) -> Optional[dict[str, Any]]:
    with db_in() as c:
        row = c.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    return dict(row) if row else None


def list_open_loans(*, limit: int = 25) -> list[dict[str, Any]]:
    """Every currently-open loan, across every borrower -- staff's
    write-off picker."""
    with db_in() as c:
        rows = c.execute(
            "SELECT id, subject, principal, interest, paid, due_at FROM loans "
            "WHERE status = 'open' ORDER BY issued_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_subject_loans(subject: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """One borrower's own open loans -- the /wallet repay picker."""
    with db_in() as c:
        rows = c.execute(
            "SELECT id, principal, interest, paid, due_at FROM loans "
            "WHERE subject = ? AND status = 'open' ORDER BY issued_at DESC LIMIT ?",
            (subject, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_paid_workers(order_id: int) -> list[str]:
    """Every distinct worker actually paid for `order_id` -- read after
    `orders.approve()` so the bot layer can sync each one's loyalty rank
    role without `core.orders` needing to know Discord exists."""
    with db_in() as c:
        rows = c.execute(
            "SELECT DISTINCT worker FROM order_claims "
            "WHERE order_id = ? AND paid_event IS NOT NULL",
            (order_id,),
        ).fetchall()
    return [r["worker"] for r in rows]
