"""Loans -- flat-limit treasury loans.

Same style as the other money-domain tests. Pins:

  [1] full lifecycle: borrow disburses immediately, no escrow -> repay
      (partial, then to completion) clears the loan and flips it to
      'repaid'.
  [2] a flat credit limit, keyed by loyalty rank, caps how much a subject
      may have outstanding across all open loans at once.
  [3] orders_blocked refuses a borrow explicitly.
  [4] write_off (staff-only) closes a loan without moving money, and
      frees the borrower's credit limit again.
  [5] a repayment attempt against someone else's loan is refused.
  [6] overpaying a loan is clamped to what is actually owed, never
      captured as a bonus payment to the treasury.
  [7] principal and interest are snapshotted at issuance: changing
      LOAN_INTEREST_PCT after a loan is out never reprices it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-loans-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import db, loans, money                              # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def raises(name: str, exc, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc:
        print(f"  ok    {name}")
    except Exception as err:                                 # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL  {name}  raised {type(err).__name__}: {err}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  did not raise {exc.__name__}")


def reset() -> None:
    with db.db() as c:
        for t in ("loans", "loyalty_overrides", "ledger_entries",
                  "ledger_holds", "wallet_flags", "idempotency", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")


db.init_db()

# ------------------------------------------------------------------ [1] full lifecycle
print("\nfull lifecycle: borrow disburses immediately -> repay clears it")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
money.mint("u:1", 10, service="owner", reason="seed for interest")  # loans don't fund their own interest
loan1 = loans.borrow("u:1", 100)
check("borrow disburses the principal on the spot", money.balance("u:1").coins == 10 + 100)
check("borrower has NO hold -- this is a disbursement, not an escrow", money.balance("u:1").held == 0)
check("interest is 10% flat on the principal", loan1["interest"] == 10)
check("owed is principal + interest", loan1["owed"] == 110)

part = loans.repay(loan1["loan_id"], "u:1", 50)
check("a partial repayment leaves the loan open", part["status"] == "open")
check("remaining is owed minus what was just paid", part["remaining"] == 60)
check("the payer's balance dropped by the repayment", money.balance("u:1").coins == 110 - 50)

full = loans.repay(loan1["loan_id"], "u:1", 60)
check("paying the rest settles the loan", full["status"] == "repaid")
with db.db() as c:
    row = c.execute("SELECT status, repaid_at FROM loans WHERE id = ?", (loan1["loan_id"],)).fetchone()
check("the loan row is 'repaid'", row["status"] == "repaid")
check("repaid_at was stamped", row["repaid_at"] is not None)

# ------------------------------------------------------------------ [2] flat credit limit
print("\na flat credit limit caps outstanding principal across all open loans")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
limit = loans.credit_limit_for("u:2")
check("recruit's limit matches CREDIT_LIMITS", limit == loans.CREDIT_LIMITS["recruit"])
loans.borrow("u:2", limit)
check("borrowing exactly the limit succeeds and maxes it out",
      loans.outstanding_owed("u:2") == limit + (limit * loans.LOAN_INTEREST_PCT) // 100)
raises("borrowing even 1 more over the limit is refused",
       loans.CreditLimitExceeded, loans.borrow, "u:2", 1)

# ------------------------------------------------------------------ [3] orders_blocked
print("\norders_blocked refuses a borrow explicitly")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
money.set_flag("u:3", "orders_blocked", service="owner", set_by="owner")
raises("a blocked subject cannot borrow", loans.OrdersBlocked, loans.borrow, "u:3", 50)
money.clear_flag("u:3", "orders_blocked", service="owner")
ok_borrow = loans.borrow("u:3", 50)
check("clearing the flag lets the borrow through", isinstance(ok_borrow["loan_id"], int))

# ------------------------------------------------------------------ [4] write_off
print("\nwrite_off closes a loan without moving money and frees the credit limit")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
limit4 = loans.credit_limit_for("u:4")
loan4 = loans.borrow("u:4", limit4)
raises("borrowing more while maxed out is refused",
       loans.CreditLimitExceeded, loans.borrow, "u:4", 1)
balance_before_writeoff = money.balance("u:4").coins
written = loans.write_off(loan4["loan_id"], actor="u:staff")
check("write_off reports success", written is True)
check("no money moved -- the borrower's balance is unchanged",
      money.balance("u:4").coins == balance_before_writeoff)
check("treasury's balance is unchanged too (the loss was already realised at disbursement)",
      money.balance("treasury:shop").coins == 100_000 - limit4)
check("outstanding_owed is 0 again -- the limit is freed", loans.outstanding_owed("u:4") == 0)
new_loan = loans.borrow("u:4", limit4)
check("the freed limit can be borrowed again", isinstance(new_loan["loan_id"], int))
raises("writing off an already-written-off loan is refused",
       loans.LoanNotOpen, loans.write_off, loan4["loan_id"], actor="u:staff")

# ------------------------------------------------------------------ [5] not your loan
print("\nrepaying someone else's loan is refused")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
loan5 = loans.borrow("u:5", 50)
raises("u:6 cannot repay u:5's loan", loans.LoanNotOpen, loans.repay, loan5["loan_id"], "u:6", 10)

# ------------------------------------------------------------------ [6] overpayment clamped
print("\noverpaying is clamped to what is actually owed")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
money.mint("u:7", 10_000, service="owner", reason="seed")   # extra funds to cover the interest
loan6 = loans.borrow("u:7", 100)  # owed = 110; u:7 now holds 10,100
result6 = loans.repay(loan6["loan_id"], "u:7", 10_000)      # only 110 is actually owed
check("only the actual amount owed was taken, not the full 10,000 typed",
      result6["paid_this_time"] == 110)
check("the loan is fully repaid", result6["status"] == "repaid")
check("the borrower kept everything beyond what was owed (10,100 - 110)",
      money.balance("u:7").coins == 10_100 - 110)
with db.db() as c:
    paid = c.execute("SELECT paid FROM loans WHERE id = ?", (loan6["loan_id"],)).fetchone()["paid"]
check("paid never exceeds principal + interest", paid == 110)

# ------------------------------------------------------------------ [7] snapshotted terms
print("\nprincipal and interest are snapshotted at issuance")
reset()
money.mint("treasury:shop", 100_000, service="owner", reason="seed")
loan7 = loans.borrow("u:8", 200)
check("interest reflects the CURRENT LOAN_INTEREST_PCT at issuance", loan7["interest"] == 20)
original_pct = loans.LOAN_INTEREST_PCT
loans.LOAN_INTEREST_PCT = 50          # simulate tuning the rate after this loan is out
try:
    with db.db() as c:
        row = c.execute("SELECT principal, interest FROM loans WHERE id = ?",
                         (loan7["loan_id"],)).fetchone()
    check("the already-issued loan's interest did NOT change", row["interest"] == 20)
finally:
    loans.LOAN_INTEREST_PCT = original_pct


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all loans tests pass")
