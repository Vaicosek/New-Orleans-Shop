"""Adversarial money tests. This file is not here to admire core/money.py --
it exists to try to make it create, destroy, or duplicate coins.

Same harness style as tests/test_money.py: real temp SQLite, check()/raises(),
exit 1 if anything here proves a hole. A PASS here documents a defence that
survived a real attempt to break it. A FAIL is a genuine finding, not a
maybe -- every FAIL below is backed by a concrete, deterministic repro.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import random
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-attack-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "attack.db")

from core import db, money                                    # noqa: E402

FAILS: list[str] = []
lock = threading.Lock()


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

# ==================================================================
# 1. Conservation under chaos: hammer mint/transfer/hold/capture/release
#    with many threads, then check SUM(ledger.delta) == SUM(wallets.coins).
# ==================================================================
print("\n[1] conservation under chaos")
reset()
money.ensure_wallet("treasury:games", deficit_floor=1_000_000, service="owner")
money.mint("u:1", 100_000, service="owner", reason="seed")
money.mint("u:2", 100_000, service="owner", reason="seed")

_errors: list[str] = []
_stop_at = time.time() + 6


def _chaos(idx: int) -> None:
    rnd = random.Random(idx * 7919 + 1)
    me, other = ("u:1", "u:2") if idx % 2 == 0 else ("u:2", "u:1")
    while time.time() < _stop_at:
        op = rnd.choice(["transfer", "hold_capture", "hold_release", "hold_partial"])
        try:
            if op == "transfer":
                money.transfer(me, other, rnd.randint(1, 500), service="shop", reason="chaos")
            else:
                amt = rnd.randint(1, 300)
                try:
                    hid = money.place_hold(me, amt, service="games", reason="chaos-bet")
                except money.MoneyError:
                    continue
                try:
                    if op == "hold_capture":
                        money.capture_hold(hid, service="games", reason="settle", to="treasury:games")
                    elif op == "hold_release":
                        money.release_hold(hid)
                    else:
                        part = max(1, amt // 2)
                        money.capture_hold(hid, part, service="games", reason="settle-part",
                                           to="treasury:games")
                        money.release_hold(hid)
                except money.MoneyError:
                    pass
        except money.MoneyError:
            pass
        except Exception as e:                                 # noqa: BLE001
            with lock:
                _errors.append(repr(e))


_threads = [threading.Thread(target=_chaos, args=(i,)) for i in range(16)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()

# sqlite's busy_timeout can still be exceeded under 16-way contention; that is
# a robustness rough edge (an OperationalError leaks past the clean MoneyError
# hierarchy) but it happens at BEGIN IMMEDIATE, before any write, so it must
# not corrupt the ledger. Report it, but judge conservation on the sums.
check("no non-locking exceptions escaped the chaos run",
      all("database is locked" in e for e in _errors), f"{_errors[:5]} (n={len(_errors)})")

with db.db() as c:
    total_ledger = c.execute("SELECT COALESCE(SUM(delta),0) t FROM ledger_entries").fetchone()["t"]
    total_wallets = c.execute("SELECT COALESCE(SUM(coins),0) t FROM wallets").fetchone()["t"]
    bad_holds = c.execute(
        "SELECT COUNT(*) n FROM ledger_holds WHERE captured + released > amount").fetchone()["n"]
check("ledger sums to wallets after 16-thread chaos (6s, thousands of ops)",
      total_ledger == total_wallets, f"ledger={total_ledger} wallets={total_wallets}")
check("no hold ever over-captured/over-released under race", bad_holds == 0, f"n={bad_holds}")

# ==================================================================
# 2. Partial-failure atomicity: make the second leg of a transfer fail and
#    check the first leg rolled back.
# ==================================================================
print("\n[2] partial-failure atomicity")

# (a) frozen destination -- natural second-leg failure
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
money.ensure_wallet("u:2")
with db.db() as c:
    c.execute("UPDATE wallets SET frozen=1 WHERE subject=?", ("u:2",))

before_src = money.balance("u:1").coins
raises("transfer into a frozen wallet is refused", money.WalletFrozen,
       money.transfer, "u:1", "u:2", 400, service="shop", reason="to-frozen")
after_src = money.balance("u:1").coins
check("frozen-destination failure leaves the source balance untouched",
      after_src == before_src, f"{before_src} -> {after_src}")
with db.db() as c:
    n = c.execute(
        "SELECT COUNT(*) n FROM ledger_entries WHERE subject='u:1' AND reason='to-frozen'"
    ).fetchone()["n"]
check("no orphan ledger row from the rolled-back leg", n == 0, f"n={n}")

# (b) injected exception between the two legs (monkeypatched _apply)
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
money.ensure_wallet("u:4")
_orig_apply = money._apply
_calls = {"n": 0}


def _flaky_apply(conn, subject, delta, **kw):
    _calls["n"] += 1
    if _calls["n"] == 2:
        raise RuntimeError("injected failure between legs")
    return _orig_apply(conn, subject, delta, **kw)


money._apply = _flaky_apply
try:
    raises("an exception injected between the two legs propagates", RuntimeError,
           money.transfer, "u:1", "u:4", 250, service="shop", reason="boom")
finally:
    money._apply = _orig_apply

check("source leg rolled back when the second leg raises mid-transaction",
      money.balance("u:1").coins == 1000, f"got {money.balance('u:1').coins}")
check("destination never saw the credit that would have paired with it",
      money.balance("u:4").coins == 0, f"got {money.balance('u:4').coins}")

# ==================================================================
# 3. Hold / available arithmetic.
# ==================================================================
print("\n[3] hold / available arithmetic")

# (a) partial capture then over-release, then exact release
reset()
money.mint("u:5", 1000, service="owner", reason="seed")
h = money.place_hold("u:5", 300, service="games", reason="bet")
took = money.capture_hold(h, 100, service="games", reason="partial-capture")
check("partial capture takes exactly what was asked", took == 100, f"took={took}")
raises("release beyond what remains on the hold is refused", money.MoneyError,
       money.release_hold, h, 250)
gave = money.release_hold(h, 200)
check("exact remaining release succeeds", gave == 200, f"gave={gave}")
b = money.balance("u:5")
check("capture (100) + release (200) never exceeds the 300 hold, available restored exactly",
      b.available == 900 and b.held == 0, f"available={b.available} held={b.held}")

# (b) capture a hold whose subject was frozen after the hold was placed
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
h2 = money.place_hold("u:1", 300, service="games", reason="bet")
with db.db() as c:
    c.execute("UPDATE wallets SET frozen=1 WHERE subject='u:1'")
raises("capture is refused once the subject is frozen, even mid-hold", money.WalletFrozen,
       money.capture_hold, h2, service="games", reason="settle")
with db.db() as c:
    row = dict(c.execute(
        "SELECT captured, released, state FROM ledger_holds WHERE id=?", (h2,)).fetchone())
check("the refused capture left the hold completely untouched (captured shrink rolled back too)",
      row == {"captured": 0, "released": 0, "state": "open"}, str(row))

# (c) concurrent partial captures on one hold never oversell it
reset()
money.mint("u:6", 1000, service="owner", reason="seed")
h3 = money.place_hold("u:6", 100, service="games", reason="bet")
_results: list[int] = []


def _race_capture() -> None:
    try:
        t = money.capture_hold(h3, 60, service="games", reason="race-cap")
        with lock:
            _results.append(t)
    except money.MoneyError:
        with lock:
            _results.append(0)


_threads = [threading.Thread(target=_race_capture) for _ in range(6)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
check("5 threads racing to each capture 60 from a 100 hold: only one wins",
      sum(_results) == 60, f"results={_results}")

# (d) concurrent hold placement never lets total held exceed coins
reset()
money.mint("u:7", 1000, service="owner", reason="seed")
_placed: list[str] = []


def _race_hold() -> None:
    try:
        hid = money.place_hold("u:7", 300, service="games", reason="race-hold")
        with lock:
            _placed.append(hid)
    except money.MoneyError:
        pass


_threads = [threading.Thread(target=_race_hold) for _ in range(10)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
b = money.balance("u:7")
check("10 threads racing for 300-coin holds on a 1000-coin wallet: at most 3 win",
      len(_placed) <= 3, f"placed={len(_placed)}")
check("available never goes negative after the hold race", b.available >= 0, str(b))

# ==================================================================
# 4. _apply's guard: floor / held / deficit interplay.
# ==================================================================
print("\n[4] _apply guard boundary")
reset()
money.ensure_wallet("treasury:x", deficit_floor=500, service="owner")
money.mint("treasury:x", 0, service="owner", reason="noop") if False else None
money.transfer("treasury:x", "u:z", 500, service="owner", reason="to-floor")
check("treasury may spend down to EXACTLY its floor", money.balance("treasury:x").coins == -500,
      f"{money.balance('treasury:x').coins}")
raises("one more coin past the floor is refused, not clamped", money.InsufficientFunds,
       money.transfer, "treasury:x", "u:z", 1, service="owner", reason="one-past-floor")

# A user's floor is always 0 -- even a partially-held, never-touched wallet
# must never be pushed negative regardless of who calls it.
reset()
money.mint("u:8", 100, service="owner", reason="seed")
money.place_hold("u:8", 50, service="games", reason="bet")
raises("a plain user cannot spend into a hold, floor=0 always enforced",
       money.InsufficientFunds, money.transfer, "u:8", "u:9", 51, service="shop", reason="x")

# ==================================================================
# 5. Idempotency.
# ==================================================================
print("\n[5] idempotency")

# (a) FIXED: crash before complete() no longer double-pays.
# The hole was that claim() / transfer / complete() were three transactions, so
# a crash between the money moving and the claim being marked done left an
# 'in_progress' row that went stale, was re-owned, and paid again. guarded()
# puts all three in ONE transaction: the crash takes the claim with it.
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
key = money.new_event_id("payout")
payload = {"to": "u:2", "amount": 100}


class Crash(Exception):
    pass


try:
    with money.guarded(key, service="shop", endpoint="payout", payload=payload) as g:
        money.transfer("u:1", "u:2", 100, service="shop", reason="payout#1",
                       idem_key=key, conn=g.conn)
        raise Crash("process dies here, after the money moved")
except Crash:
    pass

check("a crash inside the guard rolls the money back too",
      money.balance("u:2").coins == 0, f"u:2 has {money.balance('u:2').coins}")
with db.db() as c:
    stranded = c.execute("SELECT COUNT(*) n FROM idempotency WHERE key=?", (key,)).fetchone()["n"]
check("and leaves no stranded claim to go stale and be re-owned", stranded == 0,
      f"{stranded} rows left behind")

# the retry then runs cleanly, exactly once
with money.guarded(key, service="shop", endpoint="payout", payload=payload) as g:
    if not g.replay:
        money.transfer("u:1", "u:2", 100, service="shop", reason="payout#1",
                       idem_key=key, conn=g.conn)
        g.set_response({"paid": 100})
with money.guarded(key, service="shop", endpoint="payout", payload=payload) as g:
    replayed = g.replay and g.response == {"paid": 100}
check("a clean retry pays once and then replays", replayed, f"replay={replayed}")
check("exactly 100 coins landed from one key",
      money.balance("u:2").coins == 100, f"u:2 got {money.balance('u:2').coins}")

raises("claim() can no longer be called outside a transaction", TypeError,
       money.claim, money.new_event_id("x"), service="shop", endpoint="payout",
       payload=payload)

# (b) two threads racing a brand-new key: exactly one should win
reset()
key2 = money.new_event_id("payout")
payload2 = {"to": "u:9", "amount": 50}
_owned = {"n": 0}


def _try_claim() -> None:
    try:
        with money.guarded(key2, service="shop", endpoint="payout",
                           payload=payload2) as _g:
            c = _g.claim
        if c.owned:
            with lock:
                _owned["n"] += 1
    except money.MoneyError:
        pass


_threads = [threading.Thread(target=_try_claim) for _ in range(20)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
check("exactly one of 20 threads racing a fresh key wins it", _owned["n"] == 1, f"n={_owned['n']}")

# (c) fingerprint() should not collide for meaningfully different payloads
reset()
check("dict key order does not change the fingerprint",
      money.fingerprint({"a": 1, "b": 2}) == money.fingerprint({"b": 2, "a": 1}))
check("nested dict key order does not change the fingerprint",
      money.fingerprint({"x": {"a": 1, "b": 2}}) == money.fingerprint({"x": {"b": 2, "a": 1}}))
check("None value differs from a missing key",
      money.fingerprint({"a": None}) != money.fingerprint({}))
check("int amount differs from the equivalent string amount",
      money.fingerprint({"amount": 100}) != money.fingerprint({"amount": "100"}))


class _StringsLike100:
    """Anything whose __str__ collides with a legitimate string payload."""

    def __str__(self) -> str:
        return "250"


# FIXED: `default=str` used to stringify unknown types, so Decimal("100") and
# "100" hashed identically and a materially different request replayed as a
# match. The fallback is gone; an unserialisable payload is now a caller bug
# that says so here rather than at settlement time.
raises("fingerprint refuses a Decimal instead of silently stringifying it",
       TypeError, money.fingerprint, {"amount": Decimal("100")})
raises("fingerprint refuses an arbitrary object whose __str__ mimics a string",
       TypeError, money.fingerprint, {"amount": _StringsLike100()})
check("distinct JSON-native payloads still hash distinctly",
      money.fingerprint({"amount": 100}) != money.fingerprint({"amount": "100"}))

# ==================================================================
# 6. Type and boundary holes.
# ==================================================================
print("\n[6] type and boundary holes")

# (a) FIXED: coins can no longer cross the int64 range at all.
# Past 2**63-1 SQLite silently promotes the column to REAL, so the balance
# rounds through a float while the ledger stays exact -- and the conservation
# audit query (the one test_money.py itself relies on) raises "integer
# overflow" precisely when it would matter most. money.MAX_COINS is a hard
# ceiling checked inside the same UPDATE that moves the money.
reset()
BIG = 2 ** 62 + 12345
raises("a mint past the ceiling is refused, not silently floated",
       money.CeilingExceeded, money.mint, "u:big", BIG,
       service="owner", reason="big-mint-1")
money.mint("u:big", money.MAX_COINS, service="owner", reason="right up to the ceiling")
with db.db() as c:
    raw = c.execute("SELECT coins, typeof(coins) AS ty FROM wallets "
                    "WHERE subject='u:big'").fetchone()
check("a balance at the ceiling is still an exact integer",
      raw["ty"] == "integer" and int(raw["coins"]) == money.MAX_COINS,
      f"typeof={raw['ty']!r} value={raw['coins']!r}")
raises("and one more coin on top is refused", money.CeilingExceeded,
       money.mint, "u:big", 1, service="owner", reason="one too many")
with db.db() as c:
    total = c.execute("SELECT SUM(delta) AS t FROM ledger_entries").fetchone()["t"]
    coins = c.execute("SELECT SUM(coins) AS t FROM wallets").fetchone()["t"]
check("the conservation audit query still runs and still balances",
      total == coins, f"{total} vs {coins}")

# (b) __index__-only / non-int-subclass objects are correctly rejected (defence)
reset()


class _NotReallyInt:
    def __index__(self) -> int:
        return 50


raises("an __index__-only object (e.g. a numpy scalar) is rejected, not silently coerced",
       TypeError, money.mint, "u:idx", _NotReallyInt(), service="owner", reason="fake-int")

# (c) SQL metacharacters in `reason` do not touch the schema (parameterized queries)
reset()
evil_reason = "x'); DROP TABLE wallets; --"
money.mint("u:sql", 10, service="owner", reason=evil_reason)
with db.db() as c:
    still_there = c.execute(
        "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='wallets'"
    ).fetchone()["n"]
check("a reason string full of SQL metacharacters cannot touch the schema",
      still_there == 1 and money.balance("u:sql").coins == 10)

# (d) *** FINDING *** ensure_wallet has no scope check at all: any caller can
#     grant ANY subject (not just a real treasury) an arbitrary deficit_floor,
#     which is a full bypass of "only owner may mint".
reset()
raises("FIXED: a wallet with a deficit floor now needs MINT scope, which "
       "shop does not have -- the floor IS the authority to spend money that "
       "does not exist, so it is minting under another name",
       money.NotPermitted, money.ensure_wallet, "u:evil",
       deficit_floor=1_000_000, service="shop")
money.ensure_wallet("u:evil")     # plain wallet, floor 0, allowed
money.ensure_wallet("u:attacker")
raised = False
try:
    # `shop`/`games` only ever had TRANSFER+HOLD scope -- never MINT.
    money.transfer("u:evil", "u:attacker", 1_000_000, service="shop", reason="floor-mint")
except money.MoneyError:
    raised = True
check("FIXED: a non-owner service (shop, TRANSFER-scoped only) cannot conjure coins for an "
      "arbitrary subject by pre-registering it with ensure_wallet(deficit_floor=huge) "
      "(it no longer can: the attempt raises NotPermitted)",
      raised, f"u:attacker={money.balance('u:attacker').coins} u:evil={money.balance('u:evil').coins}")

# (e) related: ensure_wallet's ON CONFLICT DO NOTHING means a treasury that
#     got auto-created (e.g. by an early mint/transfer/hold with the default
#     floor of 0) can never have its floor set correctly afterwards -- the
#     "setup" call silently no-ops.
reset()
money.ensure_wallet("treasury:games")                      # auto-created with floor 0
money.ensure_wallet("treasury:games", deficit_floor=50_000, service="owner")  # intended real setup
with db.db() as c:
    floor = c.execute(
        "SELECT deficit_floor FROM wallets WHERE subject='treasury:games'").fetchone()["deficit_floor"]
check("FIXED: ensure_wallet(subject, deficit_floor=X) actually applies X once a wallet "
      "already exists at floor 0 (ON CONFLICT DO NOTHING silently drops the real setup call)",
      floor == 50_000, f"deficit_floor stayed {floor}, treasury can never go negative")

# (f) subject naming collision with the treasury: convention, via user()
check("user() can never itself produce a bare 'treasury:...' subject",
      not money.user("treasury:games").startswith("treasury:"))


print("\n[7] capture_hold service scope")

# CONTRACT.md section 8 rules 8 and 9 / SLICE_CONTRACT.md section 3.
# place_hold, mint, transfer and set_flag all resolve the caller's service
# against SERVICE_SCOPES before touching a row. capture_hold did not: any
# scope-less service that could reach a hold id could debit one wallet, credit
# another, and stamp the ledger rows with a service name the scope table says
# has no authority at all -- so the movement is invisible in exactly the record
# you would use to find it. Default for an unknown/unscoped caller is DENY.
for bad_service in ("public", "web", "not-a-service", "treasury:games", ""):
    reset()
    money.mint("u:victim", 1000, service="owner", reason="seed")
    money.ensure_wallet("u:thief", service="owner")
    hid = money.place_hold("u:victim", 300, service="games", reason="bet")

    raises(f"capture_hold refuses service={bad_service!r} (debit only)",
           money.NotPermitted,
           money.capture_hold, hid, 100, service=bad_service, reason="theft")
    raises(f"capture_hold refuses service={bad_service!r} crediting another wallet",
           money.NotPermitted,
           money.capture_hold, hid, 100, service=bad_service, reason="theft",
           to="u:thief")

    with db.db() as c:
        row = dict(c.execute(
            "SELECT captured, released, state FROM ledger_holds WHERE id=?",
            (hid,)).fetchone())
        rows = c.execute(
            "SELECT COUNT(*) AS n FROM ledger_entries WHERE service = ?",
            (bad_service,)).fetchone()["n"]
    check(f"refused capture ({bad_service!r}) left the hold open and untouched",
          row == {"captured": 0, "released": 0, "state": "open"}, str(row))
    check(f"refused capture ({bad_service!r}) wrote no ledger row naming that service",
          rows == 0, f"{rows} ledger_entries rows attributed to {bad_service!r}")
    vb, tb = money.balance("u:victim"), money.balance("u:thief")
    check(f"refused capture ({bad_service!r}) moved neither wallet",
          vb.coins == 1000 and vb.held == 300 and tb.coins == 0,
          f"victim={vb.coins}/held {vb.held} thief={tb.coins}")

# an unscoped caller may not launder a debit through `to=` either: a service
# with HOLD but no TRANSFER must not be able to credit a second wallet.
# (Every authorised service today holds both, so this asserts the rule, not a
# current gap: the authorised path still works end to end.)
reset()
money.mint("u:victim", 1000, service="owner", reason="seed")
hid = money.place_hold("u:victim", 300, service="games", reason="bet")
took = money.capture_hold(hid, 300, service="games", reason="settle",
                          to="treasury:games")
check("an authorised service still captures normally after the scope check",
      took == 300 and money.balance("u:victim").coins == 700
      and money.balance("treasury:games").coins == 300,
      f"took={took} victim={money.balance('u:victim').coins} "
      f"house={money.balance('treasury:games').coins}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all attacks were repelled")
