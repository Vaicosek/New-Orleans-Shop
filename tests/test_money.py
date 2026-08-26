"""Money tests. Real SQLite files, no mocks.

A mock cannot fail the way a WHERE clause fails, and the bugs this module
exists to prevent are all atomicity bugs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")

from core import db, money                                    # noqa: E402
from core.pricing import charge                               # noqa: E402

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
    except Exception as err:                                   # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL  {name}  raised {type(err).__name__}: {err}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  did not raise {exc.__name__}")


def reset() -> None:
    with db.db() as c:
        for t in ("ledger_entries", "ledger_holds", "wallet_flags", "idempotency",
                  "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")


db.init_db()

# ------------------------------------------------------------------ basics
print("\nbalances and transfers")
reset()
money.ensure_wallet("treasury:house", deficit_floor=10_000_000, service="owner")
money.mint("u:1", 1000, service="owner", reason="seed")
check("mint credits", money.balance("u:1").coins == 1000)
money.transfer("u:1", "u:2", 400, service="shop", reason="order#1")
check("transfer debits source", money.balance("u:1").coins == 600)
check("transfer credits target", money.balance("u:2").coins == 400)

raises("overdraw refused", money.InsufficientFunds,
       money.transfer, "u:2", "u:1", 10_000, service="shop", reason="nope")
check("refusal left balance untouched", money.balance("u:2").coins == 400)

raises("games cannot mint", money.NotPermitted,
       money.mint, "u:1", 100, service="games", reason="free money")
raises("web can do nothing", money.NotPermitted,
       money.transfer, "u:1", "u:2", 1, service="web", reason="x")
raises("float amount refused", TypeError,
       money.transfer, "u:1", "u:2", 1.5, service="shop", reason="x")
raises("bool amount refused", TypeError,
       money.transfer, "u:1", "u:2", True, service="shop", reason="x")
raises("empty reason refused", ValueError,
       money.mint, "u:1", 10, service="owner", reason="   ")

# ------------------------------------------------------------------ treasury floor
print("\ntreasury deficit floor")
reset()
money.ensure_wallet("treasury:games", deficit_floor=50_000, service="owner")
money.transfer("treasury:games", "u:1", 20_000, service="games", reason="payout")
check("treasury may go negative", money.balance("treasury:games").coins == -20_000)
raises("but not past its floor", money.InsufficientFunds,
       money.transfer, "treasury:games", "u:1", 40_000, service="games", reason="payout")
raises("a user may never go negative", money.InsufficientFunds,
       money.transfer, "u:2", "u:1", 1, service="shop", reason="x")

# ------------------------------------------------------------------ holds
print("\nholds")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
h = money.place_hold("u:1", 300, service="games", reason="bet")
b = money.balance("u:1")
check("hold does not move coins", b.coins == 1000)
check("hold reduces available", b.available == 700 and b.held == 300)
raises("cannot spend held coins", money.InsufficientFunds,
       money.transfer, "u:1", "u:2", 800, service="shop", reason="x")
money.transfer("u:1", "u:2", 700, service="shop", reason="ok")
check("can spend exactly available", money.balance("u:1").coins == 300)

money.ensure_wallet("treasury:games", deficit_floor=50_000, service="owner")
took = money.capture_hold(h, service="games", reason="lost bet", to="treasury:games")
check("capture takes the full hold", took == 300)
check("capture debits the player", money.balance("u:1").coins == 0)
check("capture credits the house", money.balance("treasury:games").coins == 300)
raises("cannot capture twice", money.MoneyError,
       money.capture_hold, h, 1, service="games", reason="again")

reset()
money.mint("u:1", 500, service="owner", reason="seed")
h2 = money.place_hold("u:1", 200, service="shop", reason="deposit")
money.release_hold(h2)
check("release restores availability", money.balance("u:1").available == 500)
raises("cannot release twice", money.MoneyError, money.release_hold, h2)

raises("hold beyond available refused", money.InsufficientFunds,
       money.place_hold, "u:1", 10_000, service="shop", reason="x")

# ------------------------------------------------------------------ self-exclusion
print("\nself-exclusion")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
money.set_flag("u:1", "gambling_blocked", service="owner", set_by="owner")
raises("blocked wallet cannot place a wager hold", money.GamblingBlocked,
       money.place_hold, "u:1", 10, service="games", reason="bet")
ok_hold = money.place_hold("u:1", 10, service="shop", reason="deposit")
check("but shop holds still work", bool(ok_hold))
raises("games cannot lift its own restriction", money.NotPermitted,
       money.clear_flag, "u:1", "gambling_blocked", service="games")
raises("games cannot set flags at all", money.NotPermitted,
       money.set_flag, "u:1", "staff", service="games", set_by="games")

# ------------------------------------------------------------------ idempotency
print("\nidempotency")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
key = money.new_event_id("payout")
payload = {"to": "u:2", "amount": 100}

with money.guarded(key, service="shop", endpoint="payout", payload=payload) as g:
    check("first claim is owned", g.claim.owned and not g.replay)
    money.transfer("u:1", "u:2", 100, service="shop", reason="order#7",
                   idem_key=key, conn=g.conn)
    g.set_response({"paid": 100})

with money.guarded(key, service="shop", endpoint="payout", payload=payload) as g:
    check("second claim replays", g.replay)
    check("replay returns the original response", g.response == {"paid": 100})
check("and paid exactly once", money.balance("u:2").coins == 100)


def _guard(k, endpoint="payout", pl=None):
    with money.guarded(k, service="shop", endpoint=endpoint,
                       payload=payload if pl is None else pl):
        pass


raises("same key, different payload is a conflict", money.IdempotencyConflict,
       _guard, key, "payout", {"to": "u:2", "amount": 999})
raises("same key, different endpoint is a conflict", money.IdempotencyConflict,
       _guard, key, "refund")

# A claim left in_progress by a still-running sibling is not re-issued.
k2 = money.new_event_id("payout")
with db.db() as c:
    money.claim(k2, service="shop", endpoint="payout", payload=payload, conn=c)
raises("an in-progress claim is not re-issued", money.IdempotencyInProgress,
       _guard, k2)

k3 = money.new_event_id("payout")
with db.db() as c:
    money.claim(k3, service="shop", endpoint="payout", payload=payload, conn=c)
    money.fail(k3, applied_unknown=True, conn=c)
raises("an out-of-band claim needs a human", money.IdempotencyUnresolved,
       _guard, k3)

# ------------------------------------------------------------------ concurrency
print("\nconcurrency (the reason none of this is a read-then-write)")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")

results: list[str] = []
lock = threading.Lock()


def spend() -> None:
    try:
        money.transfer("u:1", "u:2", 600, service="shop", reason="race")
        with lock:
            results.append("ok")
    except money.MoneyError:
        with lock:
            results.append("refused")


threads = [threading.Thread(target=spend) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("only one of eight concurrent 600-spends from 1000 won",
      results.count("ok") == 1, f"results={results}")
check("balance is exactly right after the race",
      money.balance("u:1").coins == 400 and money.balance("u:2").coins == 600,
      f"u1={money.balance('u:1').coins} u2={money.balance('u:2').coins}")

reset()
money.mint("u:1", 1000, service="owner", reason="seed")
hold_results: list[str] = []


def grab() -> None:
    try:
        money.place_hold("u:1", 600, service="games", reason="bet")
        with lock:
            hold_results.append("ok")
    except money.MoneyError:
        with lock:
            hold_results.append("refused")


threads = [threading.Thread(target=grab) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("only one of eight concurrent holds won", hold_results.count("ok") == 1,
      f"results={hold_results}")

# ------------------------------------------------------------------ ledger integrity
print("\nledger integrity")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
money.transfer("u:1", "u:2", 250, service="shop", reason="order#3")
with db.db() as c:
    total = c.execute("SELECT COALESCE(SUM(delta), 0) AS t FROM ledger_entries").fetchone()["t"]
    coins = c.execute("SELECT COALESCE(SUM(coins), 0) AS t FROM wallets").fetchone()["t"]
    unreasoned = c.execute(
        "SELECT COUNT(*) AS n FROM ledger_entries WHERE trim(reason) = ''").fetchone()["n"]
check("ledger sums to the money in the system", total == coins, f"{total} vs {coins}")
check("no unreasoned entries", unreasoned == 0)

h = money.balance("u:1")
check("every entry records the balance it produced",
      money.history("u:1")[0]["balance_after"] == h.coins)

# ------------------------------------------------------------------ pricing at the boundary
print("\npricing")
check("a full stack costs the stack price", charge(64, 300) == 300)
check("one piece of a 300 stack costs 5", charge(1, 300) == 5)
check("half a stack costs half", charge(32, 300) == 150)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all money tests pass")
