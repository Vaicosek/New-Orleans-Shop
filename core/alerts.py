"""Restock alerts with real suppression.

AbexTech's alarm recomputes from scratch every scan and its "Acknowledge"
button only disables components on one Discord message -- it writes nothing,
so the same DM repeats every scan until stock happens to move. The bug is
that acknowledgement has no state.

Here `stock_alerts.acked_until_qty` IS the state: acknowledging writes the
CURRENT quantity into it, and an alert is due only when

    qty < threshold AND (acked_until_qty IS NULL OR qty < acked_until_qty)

so it goes quiet right after an ack and speaks again only if things get
WORSE than they were at ack time. No timers, no cron state, nothing that can
drift out of sync with reality -- the only inputs are the live quantity and
the threshold.

Restocking back above threshold resets the suppression, so a later dip below
threshold fires fresh instead of staying silenced by a stale ack from before
the restock. That reset is done lazily, inside `due()`, rather than by
`catalog.set_stock` reaching into this table on every write -- the two
modules stay decoupled, and "no cron state" means there is nothing to reset
except on the next read anyway.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .db import db_in


class AlertError(RuntimeError):
    pass


class NoSuchItem(AlertError):
    pass


class NoThreshold(AlertError):
    """No stock_alerts row exists for this item -- acknowledge has nothing to write."""


def set_threshold(item_id: int, *, threshold_pct: int | None = None,
                   threshold_pieces: int | None = None,
                   conn: Optional[sqlite3.Connection] = None) -> None:
    """Configure a restock threshold, by percentage of capacity or by a raw
    piece count. Exactly one must be given -- a threshold expressed both ways
    could silently drift apart as capacity changes, so the schema only wants
    one live definition per item.
    """
    if (threshold_pct is None) == (threshold_pieces is None):
        raise ValueError("give exactly one of threshold_pct or threshold_pieces")
    if threshold_pct is not None:
        if isinstance(threshold_pct, bool) or not isinstance(threshold_pct, int):
            raise TypeError("threshold_pct must be an int")
        if not (0 < threshold_pct <= 100):
            raise ValueError("threshold_pct must be in (0, 100]")
    if threshold_pieces is not None:
        if isinstance(threshold_pieces, bool) or not isinstance(threshold_pieces, int):
            raise TypeError("threshold_pieces must be an int")
        if threshold_pieces <= 0:
            raise ValueError("threshold_pieces must be positive")

    with db_in(conn) as c:
        item = c.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise NoSuchItem(f"no such item {item_id}")
        c.execute(
            "INSERT INTO stock_alerts (item_id, threshold_pct, threshold_pieces) "
            "VALUES (:id, :pct, :pieces) "
            "ON CONFLICT(item_id) DO UPDATE SET threshold_pct = excluded.threshold_pct, "
            "threshold_pieces = excluded.threshold_pieces",
            {"id": item_id, "pct": threshold_pct, "pieces": threshold_pieces},
        )


def _effective_threshold(row: sqlite3.Row) -> int:
    if row["threshold_pieces"] is not None:
        return row["threshold_pieces"]
    return (row["capacity"] * row["threshold_pct"]) // 100


def due(*, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Items whose stock is below their threshold and not currently
    suppressed by an acknowledgement that hasn't been overtaken by worse
    stock since.

    Also performs the restock reset as a side effect: any alert whose
    current quantity is back at or above threshold has its `acked_until_qty`
    cleared, so a later dip starts from a clean slate rather than reusing a
    suppression level set before the restock happened.
    """
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT sa.item_id, sa.threshold_pct, sa.threshold_pieces, sa.acked_until_qty, "
            "       i.name, i.price_coins, i.price_unit_pieces, i.stack_size, "
            "       s.pieces AS qty, s.capacity AS capacity "
            "  FROM stock_alerts sa "
            "  JOIN items i ON i.id = sa.item_id "
            "  JOIN stock s ON s.item_id = sa.item_id "
            " WHERE i.active = 1"
        ).fetchall()

        result: list[dict[str, Any]] = []
        for r in rows:
            threshold = _effective_threshold(r)
            qty = r["qty"]
            acked = r["acked_until_qty"]

            if qty >= threshold and acked is not None:
                c.execute(
                    "UPDATE stock_alerts SET acked_until_qty = NULL WHERE item_id = ?",
                    (r["item_id"],),
                )
                acked = None

            if qty < threshold and (acked is None or qty < acked):
                result.append({
                    "item_id": r["item_id"],
                    "name": r["name"],
                    "qty": qty,
                    "threshold": threshold,
                    "capacity": r["capacity"],
                })
        return result


def acknowledge(item_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Silence the alert for `item_id` at its CURRENT quantity. The alert
    will speak again only if stock drops below this quantity, or stays
    quiet forever if stock never gets worse than this moment.
    """
    with db_in(conn) as c:
        row = c.execute(
            "SELECT s.pieces FROM stock s "
            "  JOIN stock_alerts sa ON sa.item_id = s.item_id "
            " WHERE s.item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            exists = c.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            if exists is None:
                raise NoSuchItem(f"no such item {item_id}")
            raise NoThreshold(f"item {item_id} has no restock threshold configured")

        c.execute(
            "UPDATE stock_alerts SET acked_until_qty = :q, last_fired_at = datetime('now') "
            " WHERE item_id = :id",
            {"q": row["pieces"], "id": item_id},
        )
