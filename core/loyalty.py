"""core/loyalty.py -- rank ladder: points, tiers, and what a tier pays out.

Adapted from AbexTech's `abex_tiers.py` model (a blended points+holdings
score against a five-rung ladder), fitted to what this shop's economy
actually is. AbexTech's ladder discounts PURCHASES because its customers
pay coins for items; New Orleans has no such flow -- orders here pay
*workers* out of `treasury:shop` for producing goods, and the only places a
player spends real coins are auctions and gambling losses. So the two
things that "count" toward rank are what a worker earned in order payouts,
and what a player has spent winning auction lots -- both are real value
moved through the shop, unlike a wallet balance sitting idle.

**Points are computed live from source tables, never cached.** Same
discipline as core/wagering.py's exposure query: a stored running counter
can drift from what actually happened, and re-deriving from
`order_claims`/`auction_bids` costs one aggregate query, not a migration
headache when the formula changes.

**A staff override wins outright over the computed score** -- same shape as
`money`'s wallet flags: one row, last write wins, cleared by deleting it.
This is the "somebody deserves VIP treatment, or the opposite" escape
hatch that a pure formula can never fully cover.

**What a tier actually changes**, both wired at the point the money moves,
never trusted from a stale read:
  - `payout_bonus_pct` -- added on top of an order's payout at
    `orders.approve()` time (ported from AbexTech's own "work" domain).
  - `bet_bonus_pct` -- raises the effective MAX_BET/MAX_DAILY_LOSS a
    subject gets in `core/wagering.py`, in place of AbexTech's purchase
    discount, which has nothing to attach to here.
Both are read at the moment they are needed, computed off the subject's
CURRENT tier -- a rank-up from an order does not retroactively re-price
that same order's own payout.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import money
from .db import db_in

#: 50 coins of real economic activity (a paid order-claim, or a won auction
#: bid) is 1 point. Carried over from AbexTech's LOYALTY_POINTS_DIVISOR --
#: same currency concept (a Minecraft server's gold economy) -- and is the
#: first thing to retune once real order/auction volume here is observed.
POINTS_DIVISOR = 50

#: A held wallet balance counts toward rank too, like AbexTech's savings
#: half, but capped so it can at most DOUBLE what was actually earned --
#: park a fortune and produce nothing, and rank stays Recruit, because half
#: of nothing is nothing.
POINTS_PER_COIN_HELD = 0.1
HOLDING_MATCH_CAP = 1.0

#: The ladder. Names, thresholds and payout_bonus_pct are ported directly
#: from AbexTech's LOYALTY_TIERS -- already-tuned numbers from a live
#: economy of the same shape. bet_bonus_pct has no AbexTech equivalent
#: (that project runs no betting) and is a fresh set of escalating values.
TIERS: tuple[dict, ...] = (
    {"key": "recruit", "name": "Recruit", "min_points": 0,
     "payout_bonus_pct": 0, "bet_bonus_pct": 0},
    {"key": "worker", "name": "Worker", "min_points": 1_000,
     "payout_bonus_pct": 2, "bet_bonus_pct": 10},
    {"key": "veteran", "name": "Veteran", "min_points": 5_000,
     "payout_bonus_pct": 5, "bet_bonus_pct": 25},
    {"key": "expert", "name": "Expert", "min_points": 15_000,
     "payout_bonus_pct": 8, "bet_bonus_pct": 50},
    {"key": "elite", "name": "Elite", "min_points": 40_000,
     "payout_bonus_pct": 12, "bet_bonus_pct": 100},
)
TIERS_BY_KEY = {t["key"]: t for t in TIERS}
RANK_KEYS = tuple(t["key"] for t in TIERS)


class LoyaltyError(RuntimeError):
    """Base class. A refusal, never a partial apply."""


class UnknownRank(LoyaltyError):
    pass


# ------------------------------------------------------------------ points

def _order_points(c: sqlite3.Connection, subject: str) -> int:
    row = c.execute(
        "SELECT COALESCE(SUM(paid_coins), 0) AS total FROM order_claims "
        "WHERE worker = ? AND paid_event IS NOT NULL",
        (subject,),
    ).fetchone()
    return int(row["total"]) // POINTS_DIVISOR


def _auction_points(c: sqlite3.Connection, subject: str) -> int:
    row = c.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM auction_bids "
        "WHERE subject = ? AND status = 'won'",
        (subject,),
    ).fetchone()
    return int(row["total"]) // POINTS_DIVISOR


def earned_points(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    """Points from real activity alone -- orders fulfilled as a worker, plus
    auctions won as a bidder. Never includes the holding half."""
    with db_in(conn) as c:
        return _order_points(c, subject) + _auction_points(c, subject)


def score(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """The blended score AND how it was earned, so "why am I not the next
    tier" is always a number the player can act on -- same reasoning as
    AbexTech's own `score()`, which this mirrors almost exactly."""
    with db_in(conn) as c:
        earned = earned_points(subject, conn=c)
        wallet = c.execute(
            "SELECT coins FROM wallets WHERE subject = ?", (subject,)
        ).fetchone()
    held = int(wallet["coins"]) if wallet else 0
    raw_from_holding = held * POINTS_PER_COIN_HELD
    allowed = earned * HOLDING_MATCH_CAP
    from_holding = min(raw_from_holding, allowed)
    return {
        "total": earned + from_holding,
        "from_earnings": earned,
        "from_holding": from_holding,
        "holding_capped": raw_from_holding > allowed,
    }


def tier_for(points: float) -> dict:
    """The tier a point total earns. Never returns None -- 0 points is
    Recruit."""
    current = TIERS[0]
    for t in TIERS:
        if points >= t["min_points"]:
            current = t
    return current


def next_tier(points: float) -> Optional[dict]:
    """The next rung up, or None at Elite."""
    for t in TIERS:
        if t["min_points"] > points:
            return {**t, "points_needed": t["min_points"] - points}
    return None


# ------------------------------------------------------------------ override

def override(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    with db_in(conn) as c:
        row = c.execute(
            "SELECT rank_key FROM loyalty_overrides WHERE subject = ?", (subject,)
        ).fetchone()
    return row["rank_key"] if row else None


def set_override(subject: str, rank_key: str, *, actor: str,
                  conn: Optional[sqlite3.Connection] = None) -> None:
    """Force `subject` to `rank_key`, ignoring the computed score entirely
    until cleared. Staff-only at the call site (bot/views/admin.py) -- this
    module does not itself check who is calling."""
    if rank_key not in TIERS_BY_KEY:
        raise UnknownRank(rank_key)
    with db_in(conn) as c:
        money.ensure_wallet(subject, service="owner", conn=c)
        c.execute(
            "INSERT INTO loyalty_overrides (subject, rank_key, set_by) VALUES (?, ?, ?) "
            "ON CONFLICT(subject) DO UPDATE SET rank_key = excluded.rank_key, "
            "set_by = excluded.set_by, set_at = datetime('now')",
            (subject, rank_key, actor),
        )


def clear_override(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> bool:
    """True if a override actually existed and was removed."""
    with db_in(conn) as c:
        cur = c.execute("DELETE FROM loyalty_overrides WHERE subject = ?", (subject,))
    return cur.rowcount > 0


def effective_tier(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """The rank that actually applies right now: a staff override if one is
    set, otherwise the computed tier."""
    with db_in(conn) as c:
        forced = override(subject, conn=c)
        if forced is not None:
            return TIERS_BY_KEY[forced]
        sc = score(subject, conn=c)
    return tier_for(sc["total"])


def payout_bonus_pct(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    """Read at `orders.approve()` time, off the subject's CURRENT tier --
    this order's own payout does not retroactively rank the worker up
    before pricing itself."""
    return effective_tier(subject, conn=conn)["payout_bonus_pct"]


def bet_bonus_pct(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    """Read at `wagering.check_wager()` time, raising the effective
    MAX_BET/MAX_DAILY_LOSS this subject gets."""
    return effective_tier(subject, conn=conn)["bet_bonus_pct"]


def summary(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Rank + score + progress + every benefit -- one payload for /wallet,
    the website, and the admin panel."""
    with db_in(conn) as c:
        forced = override(subject, conn=c)
        sc = score(subject, conn=c)
    tier = TIERS_BY_KEY[forced] if forced else tier_for(sc["total"])
    nxt = None if forced else next_tier(sc["total"])
    return {
        "tier": {"key": tier["key"], "name": tier["name"], "min_points": tier["min_points"]},
        "overridden": forced is not None,
        "score": sc["total"],
        "from_earnings": sc["from_earnings"],
        "from_holding": sc["from_holding"],
        "holding_capped": sc["holding_capped"],
        "next_tier": (
            {"name": nxt["name"], "min_points": nxt["min_points"],
             "points_needed": nxt["points_needed"]}
            if nxt else None
        ),
        "payout_bonus_pct": tier["payout_bonus_pct"],
        "bet_bonus_pct": tier["bet_bonus_pct"],
    }


def ladder() -> list[dict]:
    """The whole table, for a 'how ranks work' screen."""
    return [dict(t) for t in TIERS]
