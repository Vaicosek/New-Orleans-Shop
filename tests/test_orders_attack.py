"""Adversarial tests against the order/payout system.

Same style as tests/test_money.py and tests/test_shop.py: real temp SQLite,
check()/raises(), real threads. Every attack attempted here either documents
a real defence (an ok) or, for two attacks that used to break something,
now confirms the defence that was added to core/ to close it:

  - attack 3 (claim-fragmentation overpay): core/pricing.py:split_charge now
    sums payouts to exactly charge(total, price, stack), whatever the split.
  - attack 7 (self-approval via un-normalised identity): core/money.py's
    normalise_subject() is applied to every subject entering core/orders.py,
    so a cased-differently spelling of the same id no longer slips past the
    self-approval guard.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-attack-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")

from core import db, money, catalog, orders                      # noqa: E402
from core.pricing import charge                                  # noqa: E402

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
    except Exception as err:                                     # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL  {name}  raised {type(err).__name__}: {err}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  did not raise {exc.__name__}")


def reset() -> None:
    with db.db() as c:
        for t in ("order_claims", "orders", "stock_alerts", "stock", "items",
                  "ledger_entries", "ledger_holds", "wallet_flags", "idempotency",
                  "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")


db.init_db()


def seed_treasury(floor: int = 10_000_000) -> None:
    money.ensure_wallet("treasury:shop", deficit_floor=floor, service="owner")


def make_item(name: str = "Item", price: int = 300, stack: int = 64,
              barrel_slots: int = 54) -> int:
    # price_unit_pieces = stack here on purpose: every existing test quotes
    # its price "per stack", matching the pre-split single-column behaviour.
    return catalog.add_item(name, price, price_unit_pieces=stack, stack_size=stack,
                             barrel_slots=barrel_slots)


# ================================================================== Attack 1
# Double-pay: concurrent approve() on the SAME order.
print("\nattack 1a: two threads race approve() on the same order")
reset()
seed_treasury()
item_id = make_item("Cobblestone", price=640, stack=64)          # 10 coins/piece
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "u:worker1", 64)
orders.mark_fulfilled(order_id, "u:worker1", 64)

results: list[tuple[str, int]] = []
lock = threading.Lock()


def race_approve() -> None:
    try:
        r = orders.approve(order_id, "u:manager")
        with lock:
            results.append(("ok", r["paid_coins"]))
    except orders.OrderError as e:
        with lock:
            results.append((type(e).__name__, 0))


threads = [threading.Thread(target=race_approve) for _ in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()

oks = [r for r in results if r[0] == "ok"]
check("exactly one of six racing approve() calls actually paid",
      len(oks) == 1, f"results={results}")
# Credited at the PAYOUT rate, not the sell price -- the shop keeps a
# margin (CONTRACT.md 11d). What this attack guards is "exactly once",
# which the rate change does not touch.
_PAY_640 = charge(64, orders.worker_payout_for(640), 64)
check("worker1 was credited exactly once, not more",
      money.balance("u:worker1").coins == _PAY_640,
      f"got {money.balance('u:worker1').coins}, wanted {_PAY_640}")
claims = orders.list_claims(order_id)
check("the claim shows exactly one paid_event", claims[0]["paid_coins"] == _PAY_640)


# Attack 1b: approve a partially-paid order (one claim already paid via a
# prior approve, order somehow still awaiting_verification) -- does retry
# skip the paid claim and still pay the unpaid one, exactly once each?
print("\nattack 1b: retry-after-partial-pay pays the unpaid claim once, "
      "does not re-pay the paid one")
reset()
seed_treasury()
item_id = make_item("Redstone", price=128, stack=64)
order_id = orders.create_order(item_id, 20, created_by="u:owner")
orders.claim(order_id, "u:worker1", 12)
orders.claim(order_id, "u:worker2", 8)
orders.mark_fulfilled(order_id, "u:worker1", 12)
orders.mark_fulfilled(order_id, "u:worker2", 8)

# Manually pay worker1's claim out from under approve(), the way a crashed
# first attempt might have left things: paid_event set, order status left at
# awaiting_verification (simulating a crash between the claim UPDATE and the
# final "SET status='fulfilled'").
with db.db() as c:
    # The simulation must pay what approve() itself would have paid --
    # the payout rate, not the sell price -- or the 'crashed halfway'
    # state it is recreating is one approve() could never produce.
    amt = charge(12, orders.worker_payout_for(128), 64)
    evt = money.new_event_id("payout")
    c.execute("UPDATE order_claims SET paid_event = ?, paid_coins = ? "
              "WHERE order_id = ? AND worker = ?", (evt, amt, order_id, "u:worker1"))
    money.transfer("treasury:shop", "u:worker1", amt, service="shop",
                    reason="simulated partial payout", ref_kind="order",
                    ref_id=str(order_id), idem_key=evt, conn=c)
check("order is still awaiting_verification after the simulated crash",
      orders.get_order(order_id)["status"] == "awaiting_verification")

result = orders.approve(order_id, "u:manager")
check("retry only pays the outstanding claim (worker2's amount), not worker1's again",
      result["paid_coins"] == charge(8, orders.worker_payout_for(128), 64), f"got {result}")
check("worker1's balance is unchanged by the retry (no double pay)",
      money.balance("u:worker1").coins == charge(12, orders.worker_payout_for(128), 64),
      f"got {money.balance('u:worker1').coins}")
check("worker2 got paid on the retry",
      money.balance("u:worker2").coins == charge(8, orders.worker_payout_for(128), 64),
      f"got {money.balance('u:worker2').coins}")
check("order is now fulfilled", orders.get_order(order_id)["status"] == "fulfilled")


# ================================================================== Attack 2
# "Advance-per-row": does a failure partway through approve()'s loop leave
# earlier claims paid while the order stays retryable (which would then
# double-pay those on retry)? approve() runs the whole loop inside ONE
# transaction, so this attacks that assumption directly by making a later
# claim's money.transfer() fail (treasury underfunded) and checking whether
# the earlier claim's payment survives the failure.
print("\nattack 2: a mid-loop transfer failure -- does it leave earlier "
      "claims paid (setting up a double-pay on retry)?")
reset()
item_id = make_item("Gold Ingot", price=100, stack=1)             # 100 coins/piece
order_id = orders.create_order(item_id, 3, created_by="u:owner")
orders.claim(order_id, "u:worker1", 1)   # claimed_at earliest -> processed first
orders.claim(order_id, "u:worker2", 1)
orders.claim(order_id, "u:worker3", 1)
orders.mark_fulfilled(order_id, "u:worker1", 1)
orders.mark_fulfilled(order_id, "u:worker2", 1)
orders.mark_fulfilled(order_id, "u:worker3", 1)

# Treasury can afford worker1 (100) but not worker1+worker2+worker3 (300).
money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")
with db.db() as c:
    c.execute("UPDATE wallets SET coins = 150 WHERE subject = 'treasury:shop'")

raises("approve() raises when the treasury runs out mid-order",
       money.InsufficientFunds, orders.approve, order_id, "u:manager")

check("worker1 was NOT paid despite being processed first (whole loop rolled back)",
      money.balance("u:worker1").coins == 0, f"got {money.balance('u:worker1').coins}")
check("order is still awaiting_verification, not stuck half-fulfilled",
      orders.get_order(order_id)["status"] == "awaiting_verification")
claims_mid = {c["worker"]: c["paid_event"] for c in orders.list_claims(order_id)}
check("no claim carries a paid_event after the rollback",
      all(v is None for v in claims_mid.values()), f"{claims_mid}")

# Top up the treasury and retry: everyone should be paid EXACTLY once.
with db.db() as c:
    c.execute("UPDATE wallets SET coins = 1000000 WHERE subject = 'treasury:shop'")
result2 = orders.approve(order_id, "u:manager")
_PAY_3 = charge(64, orders.worker_payout_for(300), 64)
_EACH = _PAY_3 // 3
check("retry after refunding treasury pays all three claims",
      result2["paid_claims"] == 3 and result2["paid_coins"] == _PAY_3, f"{result2}")
check("worker1 paid exactly once total, never twice",
      money.balance("u:worker1").coins == _EACH,
      f"got {money.balance('u:worker1').coins}, wanted {_EACH}")
check("worker2 paid exactly once total, never twice",
      money.balance("u:worker2").coins == _EACH)
check("worker3 paid exactly once total, never twice",
      money.balance("u:worker3").coins == _EACH)


# ================================================================== Attack 3
# Pricing: charge() rounds half-up per call. Splitting one order's pieces
# across many distinct claimants (order_claims is UNIQUE(order_id, worker),
# so a single worker cannot split, but colluding/sock-puppet workers can)
# used to change the TOTAL the shop pays for the identical 64 pieces at the
# identical price -- fixed by core/pricing.py:split_charge's cumulative
# differencing, which approve() now uses instead of summing per-claim
# charge() calls.
print("\nattack 3: fragmenting a 64-piece order across many claimants must "
      "NOT inflate the total payout versus one claim of 64")
reset()
seed_treasury()
price, stack = 300, 64
# Compared at the PAYOUT rate, which is what approve() actually pays.
# The exploit this guards is about the SPLIT, not the rate: 64 pieces
# must cost the shop the same whether claimed once or sixty-four times.
single_total = charge(64, orders.worker_payout_for(price), stack)
check("sanity: one claim of 64 costs charge(64, payout_rate, 64)",
      single_total == charge(64, orders.worker_payout_for(300), 64))

item_id = make_item("Diamond", price=price, stack=stack)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
workers = [f"u:sock{i}" for i in range(64)]
for w in workers:
    orders.claim(order_id, w, 1)
for w in workers:
    orders.mark_fulfilled(order_id, w, 1)
result = orders.approve(order_id, "u:manager")

drift = result["paid_coins"] - single_total
check("DEFENCE: 64 identical pieces at the identical price cost the shop the "
      "same total (300) whether claimed as one claim of 64 or split into "
      "64 sock-puppet claims of 1 -- split_charge's cumulative differencing "
      "closes the fragmentation exploit (used to overpay by 20 coins, 6.67%)",
      result["paid_coins"] == single_total,
      f"fragmented={result['paid_coins']} single={single_total} drift={drift}")
paid_claims = orders.list_claims(order_id)
check("every sock-puppet claim was paid a non-negative amount",
      all(cl["paid_coins"] is not None and cl["paid_coins"] >= 0 for cl in paid_claims),
      f"{[cl['paid_coins'] for cl in paid_claims]}")
check("the 64 individual claim payouts still sum to the total reported",
      sum(cl["paid_coins"] for cl in paid_claims) == result["paid_coins"])


# ================================================================== Attack 4
# Price/stack_size snapshot integrity under mid-flight changes.
print("\nattack 4: repricing / restacking / cancelling mid-flight must not "
      "touch an order's charge")
reset()
seed_treasury()
item_id = make_item("Emerald", price=200, stack=64)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "u:worker1", 64)
orders.mark_fulfilled(order_id, "u:worker1", 64)

# Reprice AND change stack_size after the order exists, before approval.
# price_unit_pieces must move to 1 in the same call -- it can never exceed
# stack_size (schema CHECK) -- so this also exercises that both snapshotted
# numbers, not just price, are frozen on the order.
catalog.update_item(item_id, price_coins=999999, price_unit_pieces=1, stack_size=1)
result = orders.approve(order_id, "u:manager")
check("approve() charged the SNAPSHOTTED price/stack (200/64), not the new one",
      result["paid_coins"] == charge(64, orders.worker_payout_for(200), 64), f"got {result}")

# Cancel -> reprice -> approve must be refused (no path from cancelled to paid).
item2 = make_item("Emerald2", price=200, stack=64)
order2 = orders.create_order(item2, 64, created_by="u:owner")
orders.claim(order2, "u:worker2", 64)
orders.cancel(order2)
catalog.update_item(item2, price_coins=1)
raises("a cancelled order cannot be approved even after a reprice",
       orders.NotClaimable, orders.approve, order2, "u:manager")
check("no coins moved for the cancelled order", money.balance("u:worker2").coins == 0)


# ================================================================== Attack 5
# Stock/capacity: can claimed+delivered pieces exceed requested_pieces
# under concurrency?
print("\nattack 5: concurrent partial claims cannot push total claimed past "
      "requested_pieces")
reset()
seed_treasury()
item_id = make_item("Lapis", price=64, stack=64)
order_id = orders.create_order(item_id, 10, created_by="u:owner")

claim_out: list[str] = []


def try_claim4(worker: str) -> None:
    try:
        orders.claim(order_id, worker, 4)
        with lock:
            claim_out.append("ok")
    except orders.OrderError:
        with lock:
            claim_out.append("refused")


ts = [threading.Thread(target=try_claim4, args=(f"u:frag{i}",)) for i in range(5)]
for t in ts:
    t.start()
for t in ts:
    t.join()
total_claimed = sum(c["pieces"] for c in orders.list_claims(order_id))
check("five racing 4-piece claims on a 10-piece order never oversell it",
      total_claimed <= 10, f"total_claimed={total_claimed} results={claim_out}")


# ================================================================== Attack 6
# Zero/missing price paths beyond the already-covered "priced at 0 from
# the start": repricing to 0 AFTER the order snapshot must not zero the
# payout, and an item deactivated mid-flight must still pay (per
# catalog.deactivate_item's documented contract) rather than silently
# skipping payment.
print("\nattack 6: reprice-to-zero after snapshot, and deactivation mid-flight")
reset()
seed_treasury()
item_id = make_item("Netherite", price=500, stack=64)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "u:worker1", 64)
orders.mark_fulfilled(order_id, "u:worker1", 64)
catalog.update_item(item_id, price_coins=0)                # reprice to 0 mid-flight
result = orders.approve(order_id, "u:manager")
check("a post-snapshot reprice to 0 does NOT zero the payout (uses the snapshot)",
      result["paid_coins"] == charge(64, orders.worker_payout_for(500), 64), f"got {result}")

item2 = make_item("Netherite2", price=500, stack=64)
order2 = orders.create_order(item2, 64, created_by="u:owner")
orders.claim(order2, "u:worker2", 64)
orders.mark_fulfilled(order2, "u:worker2", 64)
catalog.deactivate_item(item2)                              # deactivate mid-flight
result2 = orders.approve(order2, "u:manager")
check("deactivating the item mid-flight does not silently skip/zero the payout",
      result2["paid_coins"] == charge(64, orders.worker_payout_for(500), 64), f"got {result2}")


# ================================================================== Attack 7
# Self-approval bypass via identity casing: the guard used to be a raw
# string comparison (order_claims.worker = :approver) with no
# normalization, so a worker claiming under one casing of their id and
# later "approving" under a different casing of the SAME id was not
# recognized as the same subject. core/money.py:normalise_subject() is now
# applied to every subject entering core/orders.py -- claim, approve,
# mark_fulfilled, create_order, and the payout target -- including the
# STORED value, not just the comparison, so this holds regardless of which
# side of the comparison is later touched by some other guard.
print("\nattack 7: self-approval guard must catch a cased spelling of the "
      "same identity, not just an exact string match")
reset()
seed_treasury()
item_id = make_item("Amethyst", price=128, stack=64)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "U:99", 64)                 # claimed/delivered as "U:99"
orders.mark_fulfilled(order_id, "U:99", 64)

raises("DEFENCE: the worker who claimed/delivered this order cannot "
       "approve its own payout under a differently-cased spelling of the same id",
       orders.SelfApproval, orders.approve, order_id, "u:99")

check("the stored claim identity was itself normalised (not just the "
      "comparison), so it reads back lowercase regardless of how it was typed",
      orders.list_claims(order_id)[0]["worker"] == "u:99",
      f"{orders.list_claims(order_id)[0]['worker']!r}")


# ================================================================== Attack 8
# THE DEAD END. A worker delivers real work; the order lands in
# `awaiting_verification`; and then NOTHING can move it. approve() raises
# ZeroPrice forever on a zero snapshot price, cancel() excluded
# awaiting_verification, and there was no way to repair the snapshot. The
# delivered labour is lost and the approval queue fills with zombies staff
# cannot clear. Under-delivered variants block the order for everyone else too.
#
# The invariant this section defends, stated once:
#   FOR EVERY NON-CLOSED ORDER THERE IS AT LEAST ONE REACHABLE TERMINAL
#   TRANSITION -- pay, or cancel. Never neither.
print("\nattack 8a: a delivered order with a ZERO snapshot price can still "
      "be cancelled (it must not be a permanent dead end)")
reset()
seed_treasury()
free_item = make_item("Freebie", price=0, stack=64)
dead1 = orders.create_order(free_item, 64, created_by="u:owner")
orders.claim(dead1, "u:worker1", 64)
orders.mark_fulfilled(dead1, "u:worker1", 64)
check("the zero-priced order really is stuck awaiting_verification",
      orders.get_order(dead1)["status"] == "awaiting_verification")
raises("approve() still refuses a zero snapshot price loudly (ZeroPrice)",
       orders.ZeroPrice, orders.approve, dead1, "u:manager")
orders.cancel(dead1, actor="u:manager", reason="unpriced, voided by staff")
check("EXIT 1: the delivered zero-price order was cancelled, not stranded",
      orders.get_order(dead1)["status"] == "cancelled",
      f"got {orders.get_order(dead1)['status']}")
check("cancelling a delivered order closes it (closed_at set)",
      orders.get_order(dead1)["closed_at"] is not None)
with db.db() as c:
    arow = c.execute("SELECT * FROM audit_actions WHERE kind = 'order.cancel' "
                     "AND target = ?", (f"order:{dead1}",)).fetchone()
check("cancelling wrote one audit_actions row naming the order",
      arow is not None, "no order.cancel audit row")
check("the cancel audit row names the unpaid delivered claim as a manual op",
      arow is not None and "u:worker1" in arow["ops_json"],
      f"ops_json={arow['ops_json'] if arow else None!r}")
check("no coins moved on a cancel", money.balance("u:worker1").coins == 0)


print("\nattack 8b: a delivered order with a ZERO snapshot price can be "
      "REPRICED by staff and then approved and paid")
reset()
seed_treasury()
free2 = make_item("Freebie2", price=0, stack=64)
dead2 = orders.create_order(free2, 64, created_by="u:owner")
orders.claim(dead2, "u:worker1", 64)
orders.mark_fulfilled(dead2, "u:worker1", 64)
raises("before the repair, approve() is a dead end (ZeroPrice)",
       orders.ZeroPrice, orders.approve, dead2, "u:manager")
repriced = orders.reprice(dead2, 300, 64, actor="u:manager")
check("reprice() returns the updated order with the new snapshot price",
      repriced["price_coins"] == 300 and repriced["price_unit_pieces"] == 64,
      f"got {repriced.get('price_coins')}/{repriced.get('price_unit_pieces')}")
check("reprice() did NOT close or otherwise move the order",
      repriced["status"] == "awaiting_verification", f"got {repriced['status']}")
res8b = orders.approve(dead2, "u:manager")
check("EXIT 2: the repriced order paid the delivered work (300)",
      res8b["paid_coins"] == charge(64, orders.worker_payout_for(300), 64), f"got {res8b}")
check("the worker actually received the 300",
      money.balance("u:worker1").coins == charge(64, orders.worker_payout_for(300), 64),
      f"got {money.balance('u:worker1').coins}")
with db.db() as c:
    rrow = c.execute("SELECT * FROM audit_actions WHERE kind = 'order.reprice' "
                     "AND target = ?", (f"order:{dead2}",)).fetchone()
check("reprice wrote one audit_actions row naming before and after",
      rrow is not None and "300" in rrow["summary"],
      f"{dict(rrow) if rrow else None}")
raises("a PAID order can never be repriced afterwards",
       orders.NotClaimable, orders.reprice, dead2, 999, 64, actor="u:manager")
raises("reprice refuses a zero price (that is the defect, not a repair)",
       ValueError, orders.reprice, dead2, 0, 64, actor="u:manager")


print("\nattack 8c: a delivered order whose TOTAL payout computes to 0 must "
      "raise loudly, never pay zero coins and close permanently")
reset()
seed_treasury()
# price 1 per 64 pieces, one delivered piece: charge(1, 1, 64) == 0.
# The snapshot price is NON-zero, so ZeroPrice does not fire -- the old code
# happily paid nobody and slammed the order shut as 'fulfilled'.
dust = make_item("Dust", price=1, stack=64)
dead3 = orders.create_order(dust, 1, created_by="u:owner")
orders.claim(dead3, "u:worker1", 1)
orders.mark_fulfilled(dead3, "u:worker1", 1)
check("sanity: this order's whole payout really does compute to 0",
      charge(1, 1, 64) == 0, f"charge(1,1,64)={charge(1, 1, 64)}")
raises("CONTRACT sec 8 rule 11: a total payout of 0 is a LOUD failure "
       "(ZeroPayout), never a silent zero-coin payment",
       orders.ZeroPayout, orders.approve, dead3, "u:manager")
check("the order is NOT closed by the refused approve -- it stays "
      "awaiting_verification so staff can reprice or cancel it",
      orders.get_order(dead3)["status"] == "awaiting_verification",
      f"got {orders.get_order(dead3)['status']}")
check("no claim was marked paid by the refused approve",
      all(cl["paid_event"] is None for cl in orders.list_claims(dead3)),
      f"{[cl['paid_event'] for cl in orders.list_claims(dead3)]}")
check("nobody was credited 0 coins in the ledger",
      money.balance("u:worker1").coins == 0)
# ... and it still has an exit, both ways.
orders.reprice(dead3, 100, 1, actor="u:manager")
res8c = orders.approve(dead3, "u:manager")
check("EXIT: after a reprice the same order pays real coins (100)",
      res8c["paid_coins"] == charge(64, orders.worker_payout_for(100), 64)
      and money.balance("u:worker1").coins == charge(64, orders.worker_payout_for(100), 64),
      f"got {res8c}")


print("\nattack 8d: a per-claim payout of 0 inside a NON-zero total is "
      "legitimate and must still be skipped, not raised")
reset()
seed_treasury()
# 1 coin per 64 pieces, 64 pieces split across 64 one-piece claims:
# split_charge telescopes to 0, 0, ... , 1, ... -- most fragments legally
# round to 0 while the ORDER total is a correct, non-zero 1. That must pay,
# not raise ZeroPayout.
frag = make_item("Fragment", price=1, stack=64)
ord8d = orders.create_order(frag, 64, created_by="u:owner")
for i in range(64):
    orders.claim(ord8d, f"u:frag{i}", 1)
for i in range(64):
    orders.mark_fulfilled(ord8d, f"u:frag{i}", 1)
res8d = orders.approve(ord8d, "u:manager")
check("the fragmented order still pays exactly charge(64, 1, 64) = 1",
      res8d["paid_coins"] == charge(64, 1, 64) == 1, f"got {res8d}")
paid8d = orders.list_claims(ord8d)
check("at least one claim legitimately rounded to a 0 payout and was skipped",
      any(cl["paid_coins"] in (None, 0) for cl in paid8d),
      f"{[cl['paid_coins'] for cl in paid8d]}")
check("the order closed as fulfilled -- a legitimate per-claim 0 is not a "
      "ZeroPayout",
      orders.get_order(ord8d)["status"] == "fulfilled")


print("\nattack 8e: cancel() is allowed from awaiting_verification and "
      "REFUSED from the two closed states")
reset()
seed_treasury()
it8e = make_item("Coal", price=300, stack=64)
paid_order = orders.create_order(it8e, 64, created_by="u:owner")
orders.claim(paid_order, "u:worker1", 64)
orders.mark_fulfilled(paid_order, "u:worker1", 64)
orders.approve(paid_order, "u:manager")
raises("a FULFILLED order cannot be cancelled (real money already moved)",
       orders.NotClaimable, orders.cancel, paid_order, actor="u:manager")
check("the fulfilled order is untouched by the refused cancel",
      orders.get_order(paid_order)["status"] == "fulfilled")

open_order = orders.create_order(it8e, 64, created_by="u:owner")
orders.cancel(open_order, actor="u:manager")
raises("a CANCELLED order cannot be cancelled twice",
       orders.NotClaimable, orders.cancel, open_order, actor="u:manager")

# The invariant, swept across every non-closed state.
print("\nattack 8f: INVARIANT -- every non-closed order has at least one "
      "reachable terminal transition")
reset()
seed_treasury()
sweep_item = make_item("Sweep", price=0, stack=64)       # worst case: price 0
states: dict[str, int] = {}
o_open = orders.create_order(sweep_item, 64, created_by="u:owner")
states["open"] = o_open
o_claimed = orders.create_order(sweep_item, 64, created_by="u:owner")
orders.claim(o_claimed, "u:w1", 32)
states["claimed"] = o_claimed
o_await = orders.create_order(sweep_item, 64, created_by="u:owner")
orders.claim(o_await, "u:w2", 64)
orders.mark_fulfilled(o_await, "u:w2", 64)
states["awaiting_verification"] = o_await
for state, oid in states.items():
    check(f"order in state {state!r} is really in that state",
          orders.get_order(oid)["status"] == state,
          f"got {orders.get_order(oid)['status']}")
    orders.cancel(oid, actor="u:manager", reason="invariant sweep")
    check(f"order in state {state!r} had a reachable exit (cancel)",
          orders.get_order(oid)["status"] == "cancelled",
          f"got {orders.get_order(oid)['status']}")


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all attack tests pass (i.e. no attack succeeded)")
