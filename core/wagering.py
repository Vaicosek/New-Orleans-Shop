"""core/wagering.py -- the ONE player-risk guard, shared by every wager.

`core/games.py` (coinflip/dice) and `core/predictions.py` (pari-mutuel
stakes) are both wagers against the SAME player-facing risk budget:
MIN_ACCOUNT_AGE_DAYS, MAX_BET per wager, MAX_DAILY_LOSS per subject per day.
This module is the one place that decides "is this subject allowed to risk
this amount right now" -- games.py and predictions.py both call
`check_wager` instead of each carrying their own copy, because two copies
drift (that drift is exactly how a prediction stake used to walk straight
past every limit a coinflip enforced).

`gambling_blocked` is enforced by `money.place_hold` itself (any service in
`money.GAMBLING_SERVICES` is gated there) and is deliberately NOT duplicated
here -- this module only ever runs before that hold is placed.

Exposure is WALLET-WIDE, not per kind. CONTRACT.md section 9 lists
MAX_DAILY_LOSS as ONE guardrail "enforced server-side at hold time", and a
loss of either kind lands in the same `gambling_day.lost` row: once a wager
of EITHER kind settles as a loss it counts against the same wallet-wide
MAX_DAILY_LOSS, which is the whole point of one cap. Open, unsettled
exposure has to be counted the same way. Bucketing it per kind was a real
bypass: a player ran casino bets and prediction stakes side by side and put
roughly 2x MAX_DAILY_LOSS at risk on one day -- ~40,000 against a 20,000
cap -- with every individual wager passing the check. A limit that reports
itself as enforced while being silently doubled is worse than no limit, so
there is exactly one bucket here now.

The check MUST run inside the very transaction that goes on to place the
hold (`db.db_in()` -> `BEGIN IMMEDIATE`, one SQLite writer lock). Both
callers already do that: two concurrent stakes cannot both read the same
"before" exposure and both slip a hold in underneath it -- the second waits
for the first to commit and then reads the updated total.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import loyalty

MAX_BET = 5_000
MAX_DAILY_LOSS = 20_000
MIN_ACCOUNT_AGE_DAYS = 3


class WageringError(RuntimeError):
    """Base class. A refusal, never a partial acceptance."""


class BetTooLarge(WageringError):
    pass


class DailyLossExceeded(WageringError):
    pass


class AccountTooNew(WageringError):
    pass


# The set of wager kinds `check_wager` will answer for. It selects the
# wording of the refusal and NOTHING else -- exposure is one wallet-wide
# bucket across every kind (see the module docstring).
_KINDS = frozenset({"games", "predictions"})

# Every open wager hold this subject owns, of any kind. Joining against the
# bet/stake tables (rather than trusting `ledger_holds.service`) keeps shop
# and order holds out of the wagering budget while still counting BOTH
# casino bets and prediction stakes, which currently share one money-service
# scope ("games").
_EXPOSURE_SQL = (
    "SELECT COALESCE(SUM(h.amount - h.captured - h.released), 0) AS exposure "
    "  FROM ledger_holds h "
    " WHERE h.subject = ? AND h.state = 'open' "
    "   AND h.id IN (SELECT hold_id FROM game_bets "
    "                UNION ALL "
    "                SELECT hold_id FROM pred_stakes)"
)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def account_age_days(conn: sqlite3.Connection, subject: str) -> int:
    """Age of `subject`'s wallet row, in whole days. No wallet yet -> 0
    (brand new): MIN_ACCOUNT_AGE_DAYS gates first contact with the economy,
    not just first contact with one particular wager kind."""
    row = conn.execute(
        "SELECT created_at FROM wallets WHERE subject = ?", (subject,)
    ).fetchone()
    if row is None:
        return 0
    created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    return (datetime.now(timezone.utc) - created).days


def check_wager(conn: sqlite3.Connection, subject: str, amount: int, *,
                 kind: str, service: str = "games") -> None:
    """Refuse `amount` for `subject` if it violates account age, MAX_BET, or
    the ONE wallet-wide MAX_DAILY_LOSS.

    MUST run in the SAME transaction that goes on to place the hold --
    callers pass the connection they are about to call `money.place_hold`
    with, so the exposure read and the hold INSERT are atomic together and
    two concurrent stakes cannot both pass.

    `kind` must be 'games' or 'predictions'. It selects the wording of the
    refusal and nothing else. `service` is IGNORED for scoping -- it is kept
    only so the existing call sites keep compiling; exposure is summed over
    every open wager hold this subject owns, of every kind.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown wagering kind {kind!r}")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("amount must be a positive int")

    if account_age_days(conn, subject) < MIN_ACCOUNT_AGE_DAYS:
        raise AccountTooNew(
            f"{subject}'s wallet is younger than MIN_ACCOUNT_AGE_DAYS={MIN_ACCOUNT_AGE_DAYS}"
        )

    # A loyalty rank raises these caps, never lowers them -- read off the
    # subject's CURRENT tier, in the SAME transaction as the exposure check
    # below, so the bonus can never be read stale against a hold that is
    # about to change it.
    bonus_pct = loyalty.bet_bonus_pct(subject, conn=conn)
    effective_max_bet = MAX_BET + (MAX_BET * bonus_pct) // 100
    effective_max_daily_loss = MAX_DAILY_LOSS + (MAX_DAILY_LOSS * bonus_pct) // 100

    if amount > effective_max_bet:
        raise BetTooLarge(
            f"{amount:,} exceeds this subject's MAX_BET of {effective_max_bet:,} "
            f"(base {MAX_BET:,}{f', +{bonus_pct}% loyalty bonus' if bonus_pct else ''})"
        )

    day = today()
    loss_row = conn.execute(
        "SELECT lost FROM gambling_day WHERE subject = ? AND day = ?", (subject, day)
    ).fetchone()
    lost_so_far = int(loss_row["lost"]) if loss_row else 0

    # gambling_day.lost only grows when a wager SETTLES, so what is currently
    # at risk in this subject's open positions has to be counted too -- across
    # BOTH kinds, or the cap is simply doubled by playing two games at once.
    exposure_row = conn.execute(_EXPOSURE_SQL, (subject,)).fetchone()
    open_exposure = int(exposure_row["exposure"])

    at_risk = lost_so_far + open_exposure + amount
    if at_risk > effective_max_daily_loss:
        raise DailyLossExceeded(
            f"{subject} would have {at_risk:,} at risk today ({lost_so_far:,} "
            f"already realized + {open_exposure:,} across all open wager "
            f"positions, casino and predictions together + {amount:,} this "
            f"{kind} wager); this subject's MAX_DAILY_LOSS is "
            f"{effective_max_daily_loss:,} (base {MAX_DAILY_LOSS:,}"
            f"{f', +{bonus_pct}% loyalty bonus' if bonus_pct else ''}) and it is "
            f"one wallet-wide cap, not one per wager kind"
        )


def record_loss(conn: sqlite3.Connection, subject: str, amount: int) -> None:
    """Add `amount` to today's REALISED loss for `subject`. Shared across
    games and predictions -- both draw down the same daily cap. A zero or
    negative amount is a no-op (a net win records no loss)."""
    if amount <= 0:
        return
    conn.execute(
        "INSERT INTO gambling_day (subject, day, staked, lost) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(subject, day) DO UPDATE SET lost = lost + excluded.lost",
        (subject, today(), amount),
    )
