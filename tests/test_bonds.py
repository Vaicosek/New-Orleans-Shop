"""Bonds -- treasury-issued, fixed-rate IOUs.

Same style as test_auctions.py/test_land.py: real SQLite files, no mocks,
check()/raises(), exit 1 on any failure. Pins:

  [1] full lifecycle: issue -> buy (money moves buyer -> treasury on the
      spot, no escrow) -> pay_coupon (proportional to units, treasury ->
      holders) -> mature (principal repaid, bond closes).
  [2] units_sold never exceeds units_total -- a compare-and-swap refuses
      an oversell rather than clamping.
  [3] orders_blocked refuses a purchase explicitly.
  [4] pay_coupon is all-or-nothing: if the treasury cannot fund every
      holder's share, NOTHING moves and next_coupon_at does not advance
      either -- the whole transaction rolls back.
  [5] pay_coupon can't be called twice for the same period (the
      compare-and-swap on next_coupon_at is the guard).
  [6] sweep_expired pays every due coupon and matures every due bond, one
      bad row never blocking the rest.
  [7] void refunds every holder's principal (not coupons already paid)
      and cannot be replayed.
  [8] a coupon amount that rounds to 0 for a tiny holding is skipped, not
      an error, and does not stop other holders getting paid.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-bonds-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import bonds, db, money                              # noqa: E402

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
        for t in ("bond_holdings", "bonds", "ledger_entries",
                  "ledger_holds", "wallet_flags", "idempotency", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")


def make_bond(name: str, *, unit_price: int = 100, units_total: int = 100,
              coupon_bps: int = 500, coupon_interval_days: int = 1,
              term_days: int = 3) -> int:
    return bonds.issue(name, unit_price, units_total, coupon_bps, coupon_interval_days,
                        term_days, created_by="u:staff")


db.init_db()

# ------------------------------------------------------------------ [1] full lifecycle
print("\nfull lifecycle: issue -> buy -> pay_coupon -> mature")
reset()
money.mint("u:1", 10_000, service="owner", reason="seed")
b1 = make_bond("Series A", unit_price=100, coupon_bps=1_000)  # 10% per coupon, easy math
bought = bonds.buy(b1, "u:1", 5)
check("buy reports the right cost", bought["cost"] == 500)
check("buyer paid the treasury on the spot", money.balance("u:1").coins == 10_000 - 500)
check("buyer has NO hold -- this is a sale, not a bid", money.balance("u:1").held == 0)
check("treasury received the sale proceeds", money.balance("treasury:shop").coins == 500)

with db.db() as c:
    row = c.execute("SELECT next_coupon_at FROM bonds WHERE id = ?", (b1,)).fetchone()
    c.execute("UPDATE bonds SET next_coupon_at = datetime('now', '-1 minute') WHERE id = ?", (b1,))
coupon = bonds.pay_coupon(b1)
check("coupon pays 10% of 5 units at 100g = 50g", coupon["paid"] == 50)
check("holder received the coupon", money.balance("u:1").coins == 10_000 - 500 + 50)
check("treasury paid out the coupon", money.balance("treasury:shop").coins == 500 - 50)

# The treasury only pocketed the 500g sale and already paid out a 50g
# coupon from it -- fund it the way a real shop treasury would be (order
# proceeds, other sales) before it owes 500g of principal back.
money.mint("treasury:shop", 500, service="owner", reason="test refill for principal")
with db.db() as c:
    c.execute("UPDATE bonds SET matures_at = datetime('now', '-1 minute') WHERE id = ?", (b1,))
result = bonds.mature(b1)
check("maturity repays principal (5 units at 100g = 500g)", result["principal_paid"] == 500)
check("holder got their principal back", money.balance("u:1").coins == 10_000 - 500 + 50 + 500)
check("treasury paid out the principal",
      money.balance("treasury:shop").coins == 500 - 50 + 500 - 500)  # sale - coupon + refill - principal
with db.db() as c:
    status = c.execute("SELECT status FROM bonds WHERE id = ?", (b1,)).fetchone()["status"]
check("the bond is 'matured'", status == "matured", status)

# ------------------------------------------------------------------ [2] no oversell
print("\nunits_sold never exceeds units_total")
reset()
money.mint("u:2", 10_000, service="owner", reason="seed")
money.mint("u:3", 10_000, service="owner", reason="seed")
b2 = make_bond("Series B", units_total=10)
bonds.buy(b2, "u:2", 8)
raises("buying more than what's left is refused", bonds.NotEnoughUnits, bonds.buy, b2, "u:3", 5)
check("the refused buyer paid nothing", money.balance("u:3").coins == 10_000)
bonds.buy(b2, "u:3", 2)
raises("a sold-out bond refuses any further purchase",
       bonds.NotEnoughUnits, bonds.buy, b2, "u:2", 1)

# ------------------------------------------------------------------ [3] orders_blocked
print("\norders_blocked refuses a purchase explicitly")
reset()
money.mint("u:4", 10_000, service="owner", reason="seed")
b3 = make_bond("Series C")
money.set_flag("u:4", "orders_blocked", service="owner", set_by="owner")
raises("a blocked subject cannot buy", bonds.OrdersBlocked, bonds.buy, b3, "u:4", 1)
money.clear_flag("u:4", "orders_blocked", service="owner")
ok_buy = bonds.buy(b3, "u:4", 1)
check("clearing the flag lets the purchase through", isinstance(ok_buy["cost"], int))

# ------------------------------------------------------------------ [4] coupon is all-or-nothing
print("\npay_coupon is all-or-nothing: an underfunded treasury moves NOTHING")
reset()
money.mint("u:5", 10_000, service="owner", reason="seed")
b4 = make_bond("Series D", unit_price=1_000, coupon_bps=5_000)  # deliberately large coupon
bonds.buy(b4, "u:5", 1)                                          # treasury now holds 1,000
with db.db() as c:
    # drain the treasury back to 0 so the coupon (500g) cannot be funded
    c.execute("UPDATE wallets SET coins = 0 WHERE subject = 'treasury:shop'")
    c.execute("UPDATE bonds SET next_coupon_at = datetime('now', '-1 minute') WHERE id = ?", (b4,))
    # captured AFTER the -1-minute rewrite: this is the value pay_coupon()
    # will try to advance past, and the value it must roll back TO on failure.
    before_next = c.execute("SELECT next_coupon_at FROM bonds WHERE id = ?", (b4,)).fetchone()["next_coupon_at"]
raises("an underfunded coupon raises rather than partially paying",
       money.MoneyError, bonds.pay_coupon, b4)
with db.db() as c:
    after_next = c.execute("SELECT next_coupon_at FROM bonds WHERE id = ?", (b4,)).fetchone()["next_coupon_at"]
check("next_coupon_at did NOT advance -- the whole transaction rolled back",
      after_next == before_next, f"before={before_next} after={after_next}")
check("holder's balance is untouched by the failed coupon", money.balance("u:5").coins == 10_000 - 1_000)

# ------------------------------------------------------------------ [5] no double-pay
print("\npay_coupon cannot be called twice for the same period")
reset()
money.mint("u:6", 10_000, service="owner", reason="seed")
b5 = make_bond("Series E", coupon_bps=1_000)
bonds.buy(b5, "u:6", 1)
with db.db() as c:
    c.execute("UPDATE bonds SET next_coupon_at = datetime('now', '-1 minute') WHERE id = ?", (b5,))
first = bonds.pay_coupon(b5)
check("the first call pays", first["paid"] == 10)
raises("a second call before the NEXT period is due finds nothing to pay",
       bonds.NothingDue, bonds.pay_coupon, b5)

# ------------------------------------------------------------------ [6] sweep_expired
print("\nsweep_expired pays every due coupon and matures every due bond")
reset()
money.mint("u:7", 10_000, service="owner", reason="seed")
money.mint("u:8", 10_000, service="owner", reason="seed")
b6a = make_bond("Series F", coupon_bps=1_000)
b6b = make_bond("Series G", coupon_bps=1_000)
bonds.buy(b6a, "u:7", 1)
bonds.buy(b6b, "u:8", 1)
with db.db() as c:
    c.execute("UPDATE bonds SET next_coupon_at = datetime('now', '-1 minute') WHERE id IN (?, ?)",
              (b6a, b6b))
    c.execute("UPDATE wallets SET coins = 0 WHERE subject = 'treasury:shop'")
# Fund enough for exactly ONE bond's coupon (each is 100 * 1 * 1000/10000 = 10g)
# -- not both. Whichever gets processed first succeeds; the other is
# refused for insufficient funds, and that refusal must not stop the sweep
# from finishing the rest.
money.mint("treasury:shop", 10, service="owner", reason="test refill")
result = bonds.sweep_expired()
check("exactly one of the two bonds' coupons paid (treasury could only fund one)",
      len(result["coupons_paid"]) == 1, result["coupons_paid"])
check("the underfunded one did not silently succeed too",
      set(result["coupons_paid"]) < {b6a, b6b})
check("treasury spent exactly what it had", money.balance("treasury:shop").coins == 0)
check("nothing matured yet -- neither bond is due", result["matured"] == [])

# ------------------------------------------------------------------ [7] void refunds principal
print("\nvoid refunds every holder's principal and cannot be replayed")
reset()
money.mint("u:9", 10_000, service="owner", reason="seed")
b7 = make_bond("Series H", unit_price=200)
bonds.buy(b7, "u:9", 3)
check("balance dropped by the purchase", money.balance("u:9").coins == 10_000 - 600)
voided = bonds.void(b7, actor="u:staff")
check("void reports it actually did something", voided is True)
check("the holder got their principal back", money.balance("u:9").coins == 10_000)
voided_again = bonds.void(b7, actor="u:staff")
check("voiding an already-voided bond is a no-op, not an error", voided_again is False)
raises("buying a voided bond is refused", bonds.BondNotOpen, bonds.buy, b7, "u:9", 1)

# ------------------------------------------------------------------ [8] rounding to 0 is skipped
print("\na coupon that rounds to 0 for a tiny holding is skipped, not an error")
reset()
money.mint("u:10", 10_000, service="owner", reason="seed")
money.mint("u:11", 10_000, service="owner", reason="seed")
b8 = make_bond("Series I", unit_price=1, units_total=1_000, coupon_bps=1)  # 1g units, 0.01% coupon
bonds.buy(b8, "u:10", 1)     # this holder's coupon share rounds to 0
bonds.buy(b8, "u:11", 500)  # a bigger holding, still 0 at these rates
with db.db() as c:
    c.execute("UPDATE bonds SET next_coupon_at = datetime('now', '-1 minute') WHERE id = ?", (b8,))
coupon8 = bonds.pay_coupon(b8)
check("a coupon call with every share rounding to 0 succeeds and pays nothing",
      coupon8["paid"] == 0)
check("u:10's balance is untouched by a 0-amount coupon", money.balance("u:10").coins == 10_000 - 1)


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all bonds tests pass")
