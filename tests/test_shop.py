"""Shop tests. Real SQLite files, no mocks -- same style as test_money.py,
because the bugs these guards prevent are all atomicity bugs a mock cannot
reproduce (a mock cannot fail the way a WHERE clause fails).
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-shop-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")

from core import audit, db, money, catalog, orders, alerts       # noqa: E402
from core.pricing import charge, price_label                     # noqa: E402

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


def seed_treasury() -> None:
    money.ensure_wallet("treasury:shop", deficit_floor=10_000_000, service="owner")


def make_item(name: str = "Honeycomb Block", price: int = 300, stack: int = 64,
              barrel_slots: int = 54) -> int:
    # price_unit_pieces = stack here on purpose: every existing test quotes
    # its price "per stack", matching the pre-split single-column behaviour.
    return catalog.add_item(name, price, price_unit_pieces=stack, stack_size=stack,
                             barrel_slots=barrel_slots)


# ------------------------------------------------------------------ catalog basics
print("\ncatalog basics")
reset()
seed_treasury()
item_id = make_item()
check("capacity is barrel_slots * stack_size", catalog.capacity_of(item_id) == 54 * 64)

label = price_label(300, 64, 64)
check("price_label states both bases", "/ stack of 64" in label and "/ piece" in label)

q = catalog.quote(item_id, 1)
check("quote uses pricing.charge, not hand rolled maths", q["total_coins"] == 5)
check("quote's label carries the price_label text", "/ stack of 64" in q["price_label"])

hits = catalog.search("honey")
check("search finds by substring, no exact name needed", any(h["id"] == item_id for h in hits))
hits2 = catalog.search("HONEYCOMB")
check("search is case-insensitive", any(h["id"] == item_id for h in hits2))

catalog.deactivate_item(item_id)
check("deactivated item drops out of search", catalog.search("honey") == [])

raises("adding a duplicate name is refused", catalog.DuplicateName,
       catalog.add_item, "Honeycomb Block", 100)

item2 = make_item("Honey Block", price=350, stack=64, barrel_slots=54)
catalog.set_stock(item2, 100)
check("set_stock reads back", catalog.get_stock(item2)["pieces"] == 100)
raises("set_stock past capacity is refused, not clamped", catalog.OverCapacity,
       catalog.set_stock, item2, 54 * 64 + 1)
catalog.adjust_stock(item2, -20)
check("adjust_stock applies the delta", catalog.get_stock(item2)["pieces"] == 80)
raises("adjust_stock below zero is refused", catalog.OverCapacity,
       catalog.adjust_stock, item2, -1000)

# ------------------------------------------------------------------ price snapshot
print("\nprice snapshot survives repricing")
reset()
seed_treasury()
item_id = make_item("Nether Wart", price=200, stack=64)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
catalog.update_item(item_id, price_coins=999)
o = orders.get_order(order_id)
check("order kept the price live when it was opened", o["price_coins"] == 200)
check("catalog shows the new price", catalog.get_item(item_id)["price_coins"] == 999)

# ------------------------------------------------------------------ claim-first concurrency
print("\nconcurrent claims for the last pieces (only one may win)")
reset()
seed_treasury()
item_id = make_item("Glowstone", price=100, stack=4)
order_id = orders.create_order(item_id, 10, created_by="u:owner")

claim_results: list[str] = []
lock = threading.Lock()


def try_claim(worker: str) -> None:
    try:
        orders.claim(order_id, worker, 10)
        with lock:
            claim_results.append("ok")
    except orders.OrderError:
        with lock:
            claim_results.append("refused")


threads = [threading.Thread(target=try_claim, args=(f"u:worker{i}",)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("only one of eight racing claims on the last 10 pieces won",
      claim_results.count("ok") == 1, f"results={claim_results}")
remaining_claims = orders.list_claims(order_id)
check("exactly one claim row exists", len(remaining_claims) == 1, f"{remaining_claims}")

# a second, distinct worker trying to claim more than remains (0 left) is refused
raises("no pieces left for a fresh claimant", orders.InsufficientRemaining,
       orders.claim, order_id, "u:latecomer", 1)

# same-worker double claim, tested with pieces still available so the UNIQUE
# constraint -- not the remaining-pieces check -- is what fires
item_dup = make_item("Sandstone", price=40, stack=64)
order_dup = orders.create_order(item_dup, 20, created_by="u:owner")
orders.claim(order_dup, "u:worker1", 5)
raises("a worker cannot claim the same order twice", orders.AlreadyClaimed,
       orders.claim, order_dup, "u:worker1", 5)

# ------------------------------------------------------------------ approve pays once
print("\napprove pays exactly once")
reset()
seed_treasury()
item_id = make_item("Ender Pearl", price=640, stack=64)     # 10 coins/piece
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "u:worker1", 64)
status = orders.mark_fulfilled(order_id, "u:worker1", 64)
check("order reaches awaiting_verification once fully delivered",
      status == "awaiting_verification")

result = orders.approve(order_id, "u:manager")
check("approve pays the full order value", result["paid_coins"] == 640)
check("worker was actually credited", money.balance("u:worker1").coins == 640)
check("order is now fulfilled", orders.get_order(order_id)["status"] == "fulfilled")

raises("re-approving an already-fulfilled order is refused", orders.NotClaimable,
       orders.approve, order_id, "u:manager")
check("balance is unchanged after the refused re-approve",
      money.balance("u:worker1").coins == 640)

claims_after = orders.list_claims(order_id)
check("the claim's paid_event was set exactly once",
      claims_after[0]["paid_event"] is not None and claims_after[0]["paid_coins"] == 640)

# ------------------------------------------------------------------ REGRESSION: order approval is audited
# core/schema.sql:87 -- audit_actions was never written by anything. Every
# money-moving path in core/ must now write one row, in the SAME transaction
# as the money it moved, naming who did it, what it moved, and how to
# reverse it (CONTRACT.md sec 4, sec 8 rule 6).
print("\nREGRESSION: order approval writes an audit_actions row")
with db.db() as c:
    audit_rows = c.execute(
        "SELECT * FROM audit_actions WHERE kind = 'order.approve' AND target = ?",
        (f"order:{order_id}",),
    ).fetchall()
check("approve() wrote exactly one audit row for this order",
      len(audit_rows) == 1, f"got {len(audit_rows)}")
if audit_rows:
    row = dict(audit_rows[0])
    check("the audit row names the real approver as actor", row["actor"] == "u:manager")
    check("money_coins matches what approve() actually paid out",
          row["money_coins"] == 640, f"got {row['money_coins']}")
    check("manual_coins is 0 -- an order payout is never a human debt",
          row["manual_coins"] == 0)
    ops = audit.get(row["id"])["ops"]
    check("ops_json records a reverse op that would claw the payout back",
          any(op.get("reverse", {}) or {} and
              op["reverse"].get("src") == "u:worker1" and
              op["reverse"].get("dst") == "treasury:shop" and
              op["reverse"].get("amount") == 640
              for op in ops),
          f"ops={ops}")

# the refused re-approve above must NOT have written a second row
with db.db() as c:
    audit_rows_after = c.execute(
        "SELECT COUNT(*) AS n FROM audit_actions WHERE kind = 'order.approve' AND target = ?",
        (f"order:{order_id}",),
    ).fetchone()["n"]
check("a refused re-approve (NotClaimable, raised before any audit write) adds no audit row",
      audit_rows_after == 1, f"got {audit_rows_after}")

# ------------------------------------------------------------------ zero price
print("\nzero-price approve raises rather than paying 0")
reset()
seed_treasury()
item_id = make_item("Free Sample", price=0, stack=64)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "u:worker1", 64)
orders.mark_fulfilled(order_id, "u:worker1", 64)
raises("a zero snapshot price refuses to pay", orders.ZeroPrice,
       orders.approve, order_id, "u:manager")
check("no coins moved on the refused zero-price approve",
      money.balance("u:worker1").coins == 0)
check("order is still awaiting_verification, not silently fulfilled",
      orders.get_order(order_id)["status"] == "awaiting_verification")

# ------------------------------------------------------------------ self-approval
print("\nself-approval refused")
reset()
seed_treasury()
item_id = make_item("Blaze Rod", price=128, stack=64)
order_id = orders.create_order(item_id, 64, created_by="u:owner")
orders.claim(order_id, "u:worker1", 64)
orders.mark_fulfilled(order_id, "u:worker1", 64)
raises("a worker who fulfilled the order cannot approve it", orders.SelfApproval,
       orders.approve, order_id, "u:worker1")
check("nothing was paid by the refused self-approval", money.balance("u:worker1").coins == 0)
ok_result = orders.approve(order_id, "u:manager")
check("a different approver can still approve it", ok_result["paid_coins"] == 128)

# ------------------------------------------------------------------ delivery bound
print("\ndelivery cannot exceed a worker's own claim")
reset()
seed_treasury()
item_id = make_item("Iron Ingot", price=64, stack=64)
order_id = orders.create_order(item_id, 20, created_by="u:owner")
orders.claim(order_id, "u:worker1", 20)
raises("delivering more than claimed is refused", orders.OverDelivery,
       orders.mark_fulfilled, order_id, "u:worker1", 21)
raises("delivering with no claim at all is refused", orders.NoSuchClaim,
       orders.mark_fulfilled, order_id, "u:stranger", 1)

# ------------------------------------------------------------------ restock alerts
print("\nrestock alerts: fire, ack silences, worse re-fires, restock resets")
reset()
seed_treasury()
item_id = make_item("Cocoa Beans", price=50, stack=64, barrel_slots=54)   # capacity 3456
alerts.set_threshold(item_id, threshold_pieces=100)

catalog.set_stock(item_id, 3456)
check("full stock is not due", alerts.due() == [])

catalog.set_stock(item_id, 50)
due1 = alerts.due()
check("dropping below threshold fires", any(d["item_id"] == item_id for d in due1))

alerts.acknowledge(item_id)
check("acknowledging at 50 silences the alert", alerts.due() == [])

catalog.set_stock(item_id, 60)
check("a smaller drop than the ack level stays silenced", alerts.due() == [])

catalog.set_stock(item_id, 20)
due2 = alerts.due()
check("stock getting WORSE than the ack level re-fires", any(d["item_id"] == item_id for d in due2))

alerts.acknowledge(item_id)
check("acknowledging again at 20 silences it", alerts.due() == [])

catalog.set_stock(item_id, 3456)
check("restocking above threshold resets suppression (still not due, it's full)",
      alerts.due() == [])

catalog.set_stock(item_id, 80)
due3 = alerts.due()
check("a fresh dip below threshold after a restock fires again (not stuck at old ack)",
      any(d["item_id"] == item_id for d in due3))

raises("acknowledging an item with no threshold configured is refused", alerts.NoThreshold,
       alerts.acknowledge, catalog.add_item("No Alert Item", 10))


# ------------------------------------------------------------------ split-charge regression
# Permanent regression guard for the claim-fragmentation overpay: however a
# 64-piece order gets split across claimants, the total the shop pays must
# equal charge(64, price, stack) exactly -- see core/pricing.py:split_charge
# and tests/test_orders_attack.py's attack 3 for the exploit this closes.
print("\nsplit-charge regression: every possible split of a 64-piece order "
      "pays the shop exactly charge(64, price, stack), never more")


def _random_partition(total: int, rng: random.Random) -> list[int]:
    """`total` split into a random number of positive parts that sum to it."""
    n = rng.randint(1, total)
    cuts = sorted(rng.sample(range(1, total), n - 1)) if n > 1 else []
    bounds = [0, *cuts, total]
    return [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]


reset()
seed_treasury()
_rng = random.Random(20260826)   # fixed seed: deterministic, reproducible failures
_AWKWARD_PRICES = [1, 7, 99, 300, 1001, 4097]   # includes exact and remainder-heavy cases
_STACK = 64
_FIXED_SPLITS = {
    "1x64": [64],
    "64x1": [1] * 64,
    "2x32": [32, 32],
    "3+61": [3, 61],
}

for _price in _AWKWARD_PRICES:
    _item_id = make_item(f"Split Regression {_price}", price=_price, stack=_STACK)
    _expected = charge(64, _price, _STACK)

    _splits = dict(_FIXED_SPLITS)
    for _i in range(3):
        _splits[f"random{_i}"] = _random_partition(64, _rng)

    for _split_name, _pieces in _splits.items():
        _order_id = orders.create_order(_item_id, 64, created_by="u:owner")
        _workers = [f"u:split_{_price}_{_split_name}_{_i}" for _i in range(len(_pieces))]
        for _w, _p in zip(_workers, _pieces):
            orders.claim(_order_id, _w, _p)
        for _w, _p in zip(_workers, _pieces):
            orders.mark_fulfilled(_order_id, _w, _p)
        _result = orders.approve(_order_id, "u:manager")
        check(f"price={_price} split={_split_name} ({len(_pieces)} claim(s), "
              f"sum(pieces)={sum(_pieces)}) pays exactly {_expected}",
              _result["paid_coins"] == _expected,
              f"got {_result['paid_coins']} pieces={_pieces}")
        # A claim can legitimately be skipped (paid_coins stays NULL) when its
        # slice of the rounding rounds to exactly 0 -- that is not a bug, so
        # treat NULL as 0 here; the only thing this guards against is a
        # negative individual payout.
        check(f"price={_price} split={_split_name}: no individual claim payout went negative",
              all((cl["paid_coins"] or 0) >= 0 for cl in orders.list_claims(_order_id)))


# ------------------------------------------------------------------ REGRESSION: the 64x split is actually exercised
# CONTRACT.md section 5's rounding table, as explicit cases, plus a fixture whose
# price_unit_pieces != stack_size. Before this block every fixture in this file
# passed price_unit_pieces = stack_size (see make_item), so the ONE case the
# split exists for was untested: swapping charge()'s divisor from
# price_unit_pieces to stack_size would have priced saplings at half and left
# the whole suite green.
print("\nREGRESSION: CONTRACT.md section 5 rounding table, in full")

check("charge(64, 300, 64) is one full stack at the quoted price",
      charge(64, 300, 64) == 300, f"got {charge(64, 300, 64)}")
check("charge(1, 300, 64) rounds 4.6875 half-up to 5",
      charge(1, 300, 64) == 5, f"got {charge(1, 300, 64)}")
check("charge(32, 300, 64) is exactly 150, no rounding",
      charge(32, 300, 64) == 150, f"got {charge(32, 300, 64)}")
check("charge(0, 300) is 0", charge(0, 300) == 0, f"got {charge(0, 300)}")
check("charge() defaults unit_pieces to STACK (64)",
      charge(1, 300) == charge(1, 300, 64) == 5)

# The worked example from CONTRACT.md section 5. This is THE case the two-column
# split exists to make impossible, and it is the assertion that discriminates:
# a divisor swapped to stack_size gives 1 here, not 2.
check("WORKED EXAMPLE: charge(64, 1, 32) == 2 -- a full 64-piece stack of "
      "saplings quoted at 1 g per 32 costs TWO gold",
      charge(64, 1, 32) == 2, f"got {charge(64, 1, 32)} -- the divisor is wrong")
check("...and charge(64, 1, 64) == 1, so the two divisors give DIFFERENT "
      "answers -- this test cannot pass with stack_size as the divisor",
      charge(64, 1, 64) == 1 and charge(64, 1, 32) != charge(64, 1, 64),
      f"32-divisor={charge(64, 1, 32)} 64-divisor={charge(64, 1, 64)}")
check("charge(32, 1, 32) == 1 -- exactly one quoted unit",
      charge(32, 1, 32) == 1, f"got {charge(32, 1, 32)}")
check("charge(1, 1, 32) == 0 -- 0.03 g rounds half-up to nothing",
      charge(1, 1, 32) == 0, f"got {charge(1, 1, 32)}")

# ------------------------------------------------------------------ sapling fixture: price_unit_pieces != stack_size
print("\nREGRESSION: a sapling fixture (1 g / 32, stacks to 64) drives the "
      "price path and the capacity path apart")
reset()
seed_treasury()

# barrel_slots=1 so capacity discriminates too: 1 * 64 = 64, never 1 * 32 = 32.
sapling = catalog.add_item("Sapling", 1, price_unit_pieces=32, stack_size=64,
                           barrel_slots=1)
sap = catalog.get_item(sapling)
check("the item stores both numbers separately, verbatim",
      sap["price_coins"] == 1 and sap["price_unit_pieces"] == 32
      and sap["stack_size"] == 64,
      f"got {sap['price_coins']}/{sap['price_unit_pieces']}/{sap['stack_size']}")

check("capacity is barrel_slots * STACK_SIZE (64), never * price_unit_pieces (32)",
      catalog.capacity_of(sapling) == 64, f"got {catalog.capacity_of(sapling)}")

check("quote divides by price_unit_pieces: 64 pieces cost 2 g, not 1",
      catalog.quote(sapling, 64)["total_coins"] == 2,
      f"got {catalog.quote(sapling, 64)['total_coins']}")
check("quote for 32 pieces is exactly 1 g",
      catalog.quote(sapling, 32)["total_coins"] == 1,
      f"got {catalog.quote(sapling, 32)['total_coins']}")
check("quote's label names the quoted unit as 32, not 'stack of 64'",
      "/ 32" in catalog.quote(sapling, 64)["price_label"]
      and "stack of" not in catalog.quote(sapling, 64)["price_label"],
      catalog.quote(sapling, 64)["price_label"])

# ...and the same divisor all the way through the order lifecycle.
sap_order = orders.create_order(sapling, 64, created_by="u:owner")
_snap = orders.get_order(sap_order)
check("create_order snapshots BOTH numbers, not one of them twice",
      _snap["price_unit_pieces"] == 32 and _snap["stack_size"] == 64,
      f"got unit={_snap['price_unit_pieces']} stack={_snap['stack_size']}")

orders.claim(sap_order, "u:sapworker", 64)
orders.mark_fulfilled(sap_order, "u:sapworker", 64)
_sap_result = orders.approve(sap_order, "u:manager")
check("approve pays 2 g for a full 64-piece sapling stack -- halving it to 1 "
      "is the exact bug the price/stack split exists to prevent",
      _sap_result["paid_coins"] == 2, f"got {_sap_result['paid_coins']}")
check("the worker was really credited 2, not 1",
      money.balance("u:sapworker").coins == 2,
      f"got {money.balance('u:sapworker').coins}")

# split_charge must telescope against price_unit_pieces too, not stack_size.
sap2 = catalog.add_item("Sapling (split)", 1, price_unit_pieces=32,
                        stack_size=64, barrel_slots=54)
for _name, _pieces in {"1x64": [64], "2x32": [32, 32], "64x1": [1] * 64,
                       "3+61": [3, 61]}.items():
    _oid = orders.create_order(sap2, 64, created_by="u:owner")
    _ws = [f"u:sap_{_name}_{_i}" for _i in range(len(_pieces))]
    for _w, _p in zip(_ws, _pieces):
        orders.claim(_oid, _w, _p)
    for _w, _p in zip(_ws, _pieces):
        orders.mark_fulfilled(_oid, _w, _p)
    _r = orders.approve(_oid, "u:manager")
    check(f"sapling split={_name} pays exactly charge(64, 1, 32) = 2",
          _r["paid_coins"] == 2, f"got {_r['paid_coins']} pieces={_pieces}")

raises("price_unit_pieces may never exceed stack_size", ValueError,
       catalog.add_item, "Bad Unit", 1, price_unit_pieces=128, stack_size=64)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all shop tests pass")
