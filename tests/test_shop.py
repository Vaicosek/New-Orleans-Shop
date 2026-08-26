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

from core import db, money, catalog, orders, alerts             # noqa: E402
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

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all shop tests pass")
