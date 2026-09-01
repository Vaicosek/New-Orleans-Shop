"""core/loans.py -- flat-limit treasury loans.

Deliberately the simple version, not AbexTech's `bank_policy.py`: no scored
credit history, no automatic collection or garnishment, no per-loan
negotiation. A player borrows directly from `treasury:shop`, self-serve, up
to a flat credit limit keyed only by their CURRENT loyalty rank
(core/loyalty.py's `effective_tier`) -- not by repayment history. Interest
and term are fixed system-wide constants (`LOAN_INTEREST_PCT`,
`LOAN_TERM_DAYS`), never negotiated per loan. See CONTRACT.md section 11c.

Lifecycle: `borrow` disburses the principal immediately (a plain
`money.transfer`, treasury -> borrower -- no escrow, there is nothing to
hold) and snapshots what is owed (principal + a flat interest amount fixed
at issuance, so a later change to `LOAN_INTEREST_PCT` never reprices a loan
already out). `repay` pays it down, any amount, any number of times, until
the balance clears and the loan flips to 'repaid'. `write_off` (staff-only)
marks an uncollectible loan closed without moving any more money -- the
treasury already lost the principal the moment it disbursed; `write_off`
only stops chasing it. It also frees the borrower's credit limit again
(`outstanding_owed` only sums 'open' loans) -- a deliberate simplicity
trade-off: this is NOT a permanent ban, so a staff member who wants one
combines `write_off` with freezing the wallet or setting `orders_blocked`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import audit, loyalty, money
from .db import db_in

SERVICE = "shop"
TREASURY = money.SERVICE_TREASURY[SERVICE]  # "treasury:shop"

# Flat, system-wide -- not negotiated per loan. Tune here; changing these
# only affects loans issued AFTER the change, since principal/interest are
# snapshotted at issuance.
LOAN_INTEREST_PCT = 10       # flat, once, on the principal -- not compounding
LOAN_TERM_DAYS = 14

# Placeholder figures -- tune to the server's actual economy. Keyed by
# core/loyalty.py's TIERS rank keys; a rank with no entry here borrows 0.
CREDIT_LIMITS: dict[str, int] = {
    "recruit": 200,
    "worker": 500,
    "veteran": 1_500,
    "expert": 4_000,
    "elite": 10_000,
}


class LoanError(RuntimeError):
    """Base class. A refusal, never a partial apply."""


class UnknownLoan(LoanError): pass
class LoanNotOpen(LoanError): pass
class CreditLimitExceeded(LoanError): pass


class OrdersBlocked(LoanError):
    """`subject` has the `orders_blocked` wallet flag. Borrowing is
    commerce, same reasoning as auctions/land/bonds' OrdersBlocked."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _loan(c: sqlite3.Connection, loan_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if row is None:
        raise UnknownLoan(loan_id)
    return row


def credit_limit_for(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    tier = loyalty.effective_tier(subject, conn=conn)
    return CREDIT_LIMITS.get(tier["key"], 0)


def outstanding_owed(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    """What `subject` still owes across every OPEN loan. Repaid loans
    contribute 0 (their `paid` already covers `principal + interest`);
    written-off loans also contribute 0 -- see the module docstring for why
    that is a deliberate simplicity trade-off, not an oversight."""
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT principal, interest, paid FROM loans WHERE subject = ? AND status = 'open'",
            (subject,),
        ).fetchall()
    return sum(max(r["principal"] + r["interest"] - r["paid"], 0) for r in rows)


# ------------------------------------------------------------------ lifecycle

def borrow(subject: str, amount: int, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise LoanError("amount must be a positive int")
    with db_in(conn) as c:
        if "orders_blocked" in money.flags(subject, conn=c):
            raise OrdersBlocked(f"{subject} has orders blocked")

        limit = credit_limit_for(subject, conn=c)
        owed = outstanding_owed(subject, conn=c)
        if owed + amount > limit:
            raise CreditLimitExceeded(
                f"{subject} already owes {owed:,}, limit is {limit:,}; "
                f"cannot borrow {amount:,} more"
            )

        interest = (amount * LOAN_INTEREST_PCT) // 100
        due_at = (datetime.now(timezone.utc)
                  + timedelta(days=LOAN_TERM_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

        # `loans.subject` FK's to `wallets(subject)` -- ensure it exists
        # BEFORE the insert, same reasoning as money.transfer ensuring both
        # legs' wallets before it ever touches a balance.
        money.ensure_wallet(subject, conn=c)
        money.ensure_wallet(TREASURY, conn=c)
        cur = c.execute(
            "INSERT INTO loans (subject, principal, interest, due_at) VALUES (?, ?, ?, ?)",
            (subject, amount, interest, due_at),
        )
        loan_id = cur.lastrowid
        money.transfer(
            TREASURY, subject, amount, service=SERVICE, reason=f"loan #{loan_id} disbursed",
            ref_kind="loan", ref_id=str(loan_id), conn=c,
        )
        audit.record(
            c, actor=subject, target=f"loan:{loan_id}", kind="loan.borrow",
            summary=f"{subject} borrowed {amount:,} as loan #{loan_id}, "
                    f"owes {amount + interest:,} by {due_at}",
            ops=[{"op": "transfer", "src": TREASURY, "dst": subject, "amount": amount,
                  "reverse": {"op": "transfer", "src": subject, "dst": TREASURY,
                              "amount": amount}}],
            money_coins=amount, manual_coins=0,
        )
        return {"loan_id": loan_id, "principal": amount, "interest": interest,
                "owed": amount + interest, "due_at": due_at}


def repay(loan_id: int, subject: str, amount: int, *,
          conn: Optional[sqlite3.Connection] = None) -> dict:
    """Pay down `loan_id` by `amount` (clamped to what is actually still
    owed -- overpaying by typo is capped, never captured as a tip to the
    treasury). Refuses a repayment against someone else's loan: this is a
    self-serve path, not a staff tool for paying on another player's
    behalf."""
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise LoanError("amount must be a positive int")
    with db_in(conn) as c:
        loan = _loan(c, loan_id)
        if loan["subject"] != subject:
            raise LoanNotOpen(f"loan {loan_id} does not belong to {subject}")
        if loan["status"] != "open":
            raise LoanNotOpen(f"loan {loan_id} is {loan['status']}, not open")

        remaining = loan["principal"] + loan["interest"] - loan["paid"]
        pay = min(amount, remaining)
        new_paid = loan["paid"] + pay
        settled = new_paid >= loan["principal"] + loan["interest"]

        money.transfer(
            subject, TREASURY, pay, service=SERVICE, reason=f"loan #{loan_id} repayment",
            ref_kind="loan", ref_id=str(loan_id), conn=c,
        )
        c.execute(
            "UPDATE loans SET paid = ?, status = ?, repaid_at = ? WHERE id = ?",
            (new_paid, "repaid" if settled else "open",
             _now() if settled else None, loan_id),
        )
        audit.record(
            c, actor=subject, target=f"loan:{loan_id}", kind="loan.repay",
            summary=f"{subject} repaid {pay:,} on loan #{loan_id}"
                    + (" (paid in full)" if settled else f", {remaining - pay:,} remaining"),
            ops=[{"op": "transfer", "src": subject, "dst": TREASURY, "amount": pay,
                  "reverse": {"op": "transfer", "src": TREASURY, "dst": subject,
                              "amount": pay}}],
            money_coins=pay, manual_coins=0,
        )
        return {"loan_id": loan_id, "paid_this_time": pay, "remaining": remaining - pay,
                "status": "repaid" if settled else "open"}


def write_off(loan_id: int, *, actor: str,
              conn: Optional[sqlite3.Connection] = None) -> bool:
    """Staff-only. Closes an uncollectible loan without moving money -- the
    treasury already lost the principal at disbursement. Idempotent in
    spirit: refuses on a loan that isn't 'open' rather than pretending to
    do something a second time."""
    with db_in(conn) as c:
        loan = _loan(c, loan_id)
        if loan["status"] != "open":
            raise LoanNotOpen(f"loan {loan_id} is {loan['status']}, not open")

        cur = c.execute(
            "UPDATE loans SET status = 'written_off', written_off_at = ?, written_off_by = ? "
            "WHERE id = ? AND status = 'open'",
            (_now(), actor, loan_id),
        )
        if cur.rowcount != 1:
            raise LoanNotOpen(f"loan {loan_id} was settled concurrently")

        remaining = loan["principal"] + loan["interest"] - loan["paid"]
        audit.record(
            c, actor=actor, target=f"loan:{loan_id}", kind="loan.write_off",
            summary=f"wrote off loan #{loan_id} ({loan['subject']}): {remaining:,} forgiven",
            ops=[], money_coins=0, manual_coins=remaining,
        )
    return True
