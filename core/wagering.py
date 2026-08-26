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

Exposure is tracked PER KIND on purpose. `gambling_day.lost` (realised loss)
is shared across both kinds: once a wager of EITHER kind settles as a loss it
counts against the same wallet-wide MAX_DAILY_LOSS, which is the whole point
of one cap. But *open, unsettled* exposure is scoped to the kind being
checked -- an open prediction stake is not casino risk and must never lock a
subject out of coinflip/dice (and vice-versa); it will count once, correctly,
when it actually settles and lands in `gambling_day` via `record_loss`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

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


# Which open-position table counts as "exposure" for each wager kind. Joining
# against the kind's own bet/stake table (rather than trusting a shared
# ledger_holds.service string) is what keeps the two kinds from bleeding into
# each other even though both currently hold under the same money-service
# scope ("games").
_EXPOSURE_JOIN = {
    "games": "JOIN game_bets b ON b.hold_id = h.id",
    "predictions": "JOIN pred_stakes s ON s.hold_id = h.id",
}


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
    MAX_DAILY_LOSS. MUST run in the SAME transaction that goes on to place
    the hold (see the exposure comment below for why) -- callers pass the
    connection they are about to call `money.place_hold` with.

    `kind` picks which open-position table counts toward exposure: 'games'
    for coinflip/dice, 'predictions' for pari-mutuel stakes. `service` is the
    `ledger_holds.service` value those holds were placed under (both kinds
    currently share "games").
    """
    if kind not in _EXPOSURE_JOIN:
        raise ValueError(f"unknown wagering kind {kind!r}")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("amount must be a positive int")

    if account_age_days(conn, subject) < MIN_ACCOUNT_AGE_DAYS:
        raise AccountTooNew(
            f"{subject}'s wallet is younger than MIN_ACCOUNT_AGE_DAYS={MIN_ACCOUNT_AGE_DAYS}"
        )

    if amount > MAX_BET:
        raise BetTooLarge(f"{amount:,} exceeds MAX_BET {MAX_BET:,}")

    day = today()
    loss_row = conn.execute(
        "SELECT lost FROM gambling_day WHERE subject = ? AND day = ?", (subject, day)
    ).fetchone()
    lost_so_far = int(loss_row["lost"]) if loss_row else 0

    # gambling_day.lost only grows when a wager SETTLES. Without also
    # counting what is currently at risk in this subject's OWN open `kind`
    # positions, a player could open many wagers of the same kind and stack
    # up to MAX_BET in each before settling any -- none of it visible to the
    # cap until too late. This read and the hold INSERT that follows (in the
    # caller) run inside the SAME transaction (db_in()'s BEGIN IMMEDIATE) --
    # a single SQLite writer lock -- so a concurrent check_wager() for the
    # same subject cannot read this same "before" exposure and also slip its
    # own hold in underneath this one; it simply waits for this transaction
    # to commit (or roll back) and then reads the updated total.
    join = _EXPOSURE_JOIN[kind]
    exposure_row = conn.execute(
        f"SELECT COALESCE(SUM(h.amount - h.captured - h.released), 0) AS exposure "
        f"FROM ledger_holds h {join} "
        f"WHERE h.subject = ? AND h.service = ? AND h.state = 'open'",
        (subject, service),
    ).fetchone()
    open_exposure = int(exposure_row["exposure"])

    at_risk = lost_so_far + open_exposure + amount
    if at_risk > MAX_DAILY_LOSS:
        raise DailyLossExceeded(
            f"{subject} would have {at_risk:,} at risk today ({lost_so_far:,} "
            f"already realized + {open_exposure:,} in other open {kind} "
            f"positions + {amount:,} this wager); MAX_DAILY_LOSS is "
            f"{MAX_DAILY_LOSS:,}"
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
