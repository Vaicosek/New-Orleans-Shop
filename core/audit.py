"""core/audit.py -- who did what, and how to undo it.

CONTRACT.md sec 4: `audit_actions` is "what happened, who did it, and the
reverse ops to undo it." Sec 8 rule 6: "the audit row and the balance write
commit in the same transaction. Not a best-effort side call." This module is
the one place that writes that row -- `record()` takes the connection that
is ALREADY carrying the money write for this action and inserts into the
same transaction, never its own.

`money_coins` is what the system moved automatically (a `money.transfer` /
`money.capture_hold` this same action already performed). `manual_coins` is
what a human still owes by hand -- a debt the ledger could not fund
automatically (see `core/games.py`'s pending-payout path, where a win the
house cannot currently fund becomes a `manual_coins` debt instead of an
error that destroys the round). They are separate columns on purpose: a
confirm screen, or a staff /admin ledger view, must be able to show both
without doing arithmetic on `ops_json`.

`ops_json` is a list of small dicts, each naming the ledger primitive that
ran and, under `"reverse"`, the primitive that would undo it -- concrete
enough for a human (or a future executor) to act on directly. There is no
generic "run these reverse ops" function here: writing a believable reverse
recipe for every action is the fix this module makes; actually executing one
is a deliberate, separate, staff-triggered step, not something that runs on
its own.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .db import db_in


def record(conn: sqlite3.Connection, *, actor: str, target: str, kind: str,
           summary: str, ops: list[dict[str, Any]],
           money_coins: int = 0, manual_coins: int = 0,
           action_key: str | None = None) -> int:
    """Write one `audit_actions` row for one action.

    `conn` is REQUIRED and must be the same transaction that just performed
    (or decided not to perform) the money move this row describes -- passing
    a fresh connection here is exactly the "best-effort side call" CONTRACT.md
    sec 8 rule 6 forbids.

    `action_key`, when given, makes a repeat call with the SAME key a no-op
    (`ON CONFLICT DO NOTHING`) instead of a duplicate row, so a caller whose
    surrounding action is itself replay-safe (e.g. `predictions.resolve`,
    keyed on `resolve_event`) does not also double up the audit trail on a
    replay.
    """
    if not actor or not actor.strip():
        raise ValueError("audit.record needs a non-empty actor")
    if not target or not target.strip():
        raise ValueError("audit.record needs a non-empty target")
    if not summary or not summary.strip():
        raise ValueError("audit.record needs a non-empty summary")
    if not kind or not kind.strip():
        raise ValueError("audit.record needs a non-empty kind")

    ops_json = json.dumps(ops, sort_keys=True, separators=(",", ":"), default=str)

    if action_key:
        cur = conn.execute(
            "INSERT INTO audit_actions "
            "(actor, target, kind, summary, money_coins, manual_coins, ops_json, action_key) "
            "VALUES (:actor, :target, :kind, :summary, :money_coins, :manual_coins, "
            ":ops_json, :action_key) "
            "ON CONFLICT(action_key) DO NOTHING",
            {"actor": actor, "target": target, "kind": kind, "summary": summary,
             "money_coins": money_coins, "manual_coins": manual_coins,
             "ops_json": ops_json, "action_key": action_key},
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT id FROM audit_actions WHERE action_key = ?", (action_key,)
            ).fetchone()
            return int(row["id"])
        return int(cur.lastrowid)

    cur = conn.execute(
        "INSERT INTO audit_actions (actor, target, kind, summary, money_coins, manual_coins, ops_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (actor, target, kind, summary, money_coins, manual_coins, ops_json),
    )
    return int(cur.lastrowid)


def get(action_id: int, *, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any] | None:
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM audit_actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["ops"] = json.loads(d["ops_json"]) if d.get("ops_json") else []
    return d


def pending_debts(*, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Every audit row that still owes a human-paid `manual_coins` amount and
    has not been marked reversed/settled. This IS the visibility CONTRACT.md
    demands for money that is owed -- a debt the ledger could not fund
    automatically lives here, in the data, not in a silently-swallowed
    exception."""
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT * FROM audit_actions WHERE manual_coins > 0 AND reversed_at IS NULL "
            "ORDER BY ts"
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["ops"] = json.loads(d["ops_json"]) if d.get("ops_json") else []
        out.append(d)
    return out
