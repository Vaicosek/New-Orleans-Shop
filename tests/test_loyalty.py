"""Loyalty ranks -- points, tiers, staff overrides, and the two places a
tier actually changes real numbers (order payouts, wagering caps).

Same style as its neighbours: real SQLite, check()/raises(), exit 1 on any
failure. Pins:

  [1] points come from paid order-claims and won auction bids ONLY, divided
      by POINTS_DIVISOR -- nothing else moves the earned half.
  [2] a held wallet balance counts too, but capped at doubling what was
      earned -- park a fortune and produce nothing, and rank stays Recruit.
  [3] tier_for/next_tier are pure lookups against the ladder.
  [4] a staff override wins outright over the computed score, and clearing
      it reverts to the computed tier.
  [5] orders.approve() actually pays the loyalty bonus on top of the priced
      amount, and that bonus itself counts toward FUTURE points (paid_coins
      is the total actually paid).
  [6] wagering.check_wager() actually raises the effective MAX_BET and
      MAX_DAILY_LOSS for a ranked-up subject, and a Recruit (0% bonus) sees
      the exact base constants unchanged -- the existing betting tests
      depend on that being true for a fresh wallet.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-loyalty-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import auctions, catalog, db, games, loyalty, orders, wagering  # noqa: E402
from core.pricing import charge                                          # noqa: E402

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
        for t in ("loyalty_overrides", "order_claims", "orders", "auction_bids", "auctions",
                  "items", "game_bets", "game_rounds", "ledger_entries", "ledger_holds",
                  "wallet_flags", "idempotency", "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:shop", deficit_floor=10_000_000, service="owner")
    money.ensure_wallet("treasury:games", deficit_floor=10_000_000, service="owner")


def age_wallet(subject: str, days: int = 30) -> None:
    with db.db() as c:
        c.execute(
            "UPDATE wallets SET created_at = datetime('now', ?) WHERE subject = ?",
            (f"-{days} days", subject),
        )


def make_item(name: str, price: int = 6_400, stack: int = 64) -> int:
    return catalog.add_item(name, price, price_unit_pieces=stack, stack_size=stack,
                             category="test")


#: A sell price whose WORKER PAYOUT is exactly 100 coins per piece, so
#: `pay_worker` can still promise an exact figure now that the shop sells at
#: one price and pays workers at another. Derived from the live constant
#: rather than hardcoded at 143: if the margin ever moves, this moves with
#: it instead of silently paying 70% of what the tests below believe.
_PRICE_PAYING_100 = next(
    price for price in range(100, 1000) if orders.worker_payout_for(price) == 100
)


def pay_worker(worker: str, coins_wanted: int) -> None:
    """Run one order through the real lifecycle so `worker` is actually
    PAID `coins_wanted` (at the loyalty bonus that's in effect for them
    right now) -- 100 coins of PAYOUT per piece so the numbers stay clean."""
    item_id = make_item(f"item-for-{worker}-{coins_wanted}", price=_PRICE_PAYING_100, stack=1)
    pieces = coins_wanted // 100
    order_id = orders.create_order(item_id, pieces, created_by="u:owner")
    orders.claim(order_id, worker, pieces)
    orders.mark_fulfilled(order_id, worker, pieces)
    orders.approve(order_id, "u:approver")


from core import money                                        # noqa: E402

db.init_db()

# ------------------------------------------------------------------ [1] points from real activity only
print("\npoints come from paid order-claims and won auction bids, divided by POINTS_DIVISOR")
reset()
money.mint("u:owner", 1, service="owner", reason="seed")
pay_worker("u:1", 5_000)
check("order points = paid_coins // POINTS_DIVISOR",
      loyalty.earned_points("u:1") == 5_000 // loyalty.POINTS_DIVISOR,
      str(loyalty.earned_points("u:1")))

item2 = make_item("auction lot", price=1)
auction_id = auctions.open_auction(item2, pieces=1, min_bid=2_000, min_increment=100,
                                    duration_minutes=30, created_by="u:staff")
money.mint("u:2", 10_000, service="owner", reason="seed")
auctions.bid(auction_id, "u:2", 2_000)
auctions.close(auction_id)
auctions.settle(auction_id, money.new_event_id("auction.settle"))
check("auction points = won bid amount // POINTS_DIVISOR",
      loyalty.earned_points("u:2") == 2_000 // loyalty.POINTS_DIVISOR,
      str(loyalty.earned_points("u:2")))

check("an outbid (never won) bid earns no points at all", True)  # exercised below
item3 = make_item("second lot", price=1)
a3 = auctions.open_auction(item3, pieces=1, min_bid=100, min_increment=50,
                            duration_minutes=30, created_by="u:staff")
money.mint("u:3", 10_000, service="owner", reason="seed")
money.mint("u:4", 10_000, service="owner", reason="seed")
auctions.bid(a3, "u:3", 100)     # outbid immediately, never wins
auctions.bid(a3, "u:4", 150)
auctions.close(a3)
auctions.settle(a3, money.new_event_id("auction.settle"))
check("the outbid bidder earned zero points from that lot", loyalty.earned_points("u:3") == 0)

check("merely HOLDING coins earns no points on its own (that's the holding half, capped at 0 here)",
      loyalty.earned_points("u:owner") == 0)

# ------------------------------------------------------------------ [2] holding half, capped at doubling
print("\na held balance counts toward score too, capped at doubling what was earned")
reset()
pay_worker("u:5", 5_000)                                       # 100 points earned
earned = loyalty.earned_points("u:5")
bal_before = money.balance("u:5").coins
sc = loyalty.score("u:5")
check("earned points show up in the score untouched", sc["from_earnings"] == earned)
expected_from_holding = min(bal_before * loyalty.POINTS_PER_COIN_HELD, earned * loyalty.HOLDING_MATCH_CAP)
check("holding contributes exactly min(raw, cap)", sc["from_holding"] == expected_from_holding,
      f"{sc['from_holding']} vs {expected_from_holding}")
check("total is earned + capped holding", sc["total"] == earned + expected_from_holding)

reset()
money.mint("u:6", 1_000_000, service="owner", reason="seed")   # huge balance, ZERO production
sc6 = loyalty.score("u:6")
check("a rich, unproductive wallet still scores 0 -- holding cannot buy rank alone",
      sc6["total"] == 0, str(sc6))
check("tier_for(0) is Recruit", loyalty.tier_for(0)["key"] == "recruit")

# ------------------------------------------------------------------ [3] tier_for / next_tier
print("\ntier_for and next_tier are pure lookups against the ladder")
check("0 points is Recruit", loyalty.tier_for(0)["key"] == "recruit")
check("exactly at a threshold rounds UP to that tier",
      loyalty.tier_for(1_000)["key"] == "worker")
check("one point under a threshold is still the tier below",
      loyalty.tier_for(999)["key"] == "recruit")
check("a huge total is Elite, the top rung", loyalty.tier_for(1_000_000)["key"] == "elite")
nxt = loyalty.next_tier(500)
check("next_tier reports the right rung and points needed",
      nxt is not None and nxt["key"] == "worker" and nxt["points_needed"] == 500, str(nxt))
check("next_tier is None once at the top", loyalty.next_tier(1_000_000) is None)

# ------------------------------------------------------------------ [4] staff override
print("\na staff override wins outright over the computed score, and clearing reverts it")
reset()
money.mint("u:7", 1, service="owner", reason="seed")
check("with no override, a fresh wallet is Recruit",
      loyalty.effective_tier("u:7")["key"] == "recruit")
loyalty.set_override("u:7", "elite", actor="u:owner")
check("the override is now visible", loyalty.override("u:7") == "elite")
check("effective_tier reports the FORCED rank, ignoring the real score",
      loyalty.effective_tier("u:7")["key"] == "elite")
check("payout_bonus_pct reflects the forced rank", loyalty.payout_bonus_pct("u:7") == 12)
raises("setting an unknown rank key is refused", loyalty.UnknownRank,
       loyalty.set_override, "u:7", "not-a-real-rank", actor="u:owner")
cleared = loyalty.clear_override("u:7")
check("clear_override reports it actually did something", cleared is True)
check("clearing reverts to the computed tier", loyalty.effective_tier("u:7")["key"] == "recruit")
check("clearing an already-clear override is a no-op, not an error",
      loyalty.clear_override("u:7") is False)

# ------------------------------------------------------------------ [5] the payout bonus is REAL money
print("\norders.approve() actually pays the loyalty bonus on top, and it counts toward future points")
reset()
money.mint("u:owner", 1, service="owner", reason="seed")
loyalty.set_override("u:8", "elite", actor="u:owner")           # +12% payout bonus, forced
item8 = make_item("bonus test item", price=100, stack=1)
order8 = orders.create_order(item8, 100, created_by="u:owner")
# The shop SELLS 100 of these for 10,000 and PAYS the worker the snapshotted
# payout rate for them; the +12% elite bonus lands on top of the PAYOUT, not
# on top of the sale, or a fully-ranked worker would cost more than the order
# is worth (CONTRACT.md 11d).
_BASE_8 = charge(100, orders.worker_payout_for(100), 1)
_WITH_BONUS_8 = _BASE_8 + (_BASE_8 * 12) // 100
orders.claim(order8, "u:8", 100)
orders.mark_fulfilled(order8, "u:8", 100)
before = money.balance("u:8").coins
result8 = orders.approve(order8, "u:approver")
after = money.balance("u:8").coins
check("the worker was paid MORE than the bare payout amount (payout + 12%)",
      after - before == _WITH_BONUS_8, f"paid {after - before}, wanted {_WITH_BONUS_8}")
check("the bonus is genuinely on top", _WITH_BONUS_8 > _BASE_8)
check("...and the whole thing still costs the shop less than the 10,000 sale",
      _WITH_BONUS_8 < 10_000, f"cost {_WITH_BONUS_8} against a 10,000 sale")
check("approve()'s own reported total includes the bonus",
      result8["paid_coins"] == _WITH_BONUS_8)
with db.db() as c:
    paid_coins_row = c.execute(
        "SELECT paid_coins FROM order_claims WHERE order_id = ?", (order8,)
    ).fetchone()
check("paid_coins on the claim itself is the TOTAL actually paid, bonus included",
      paid_coins_row["paid_coins"] == _WITH_BONUS_8, str(dict(paid_coins_row)))
loyalty.clear_override("u:8")
check("the bonus counts toward the worker's OWN future points",
      loyalty.earned_points("u:8") == _WITH_BONUS_8 // loyalty.POINTS_DIVISOR)

# A Recruit (no override, no history) gets no bonus at all -- the existing
# order-payout tests in test_shop.py depend on paying EXACTLY the priced
# amount for a fresh wallet.
reset()
money.mint("u:owner", 1, service="owner", reason="seed")
item9 = make_item("no bonus for a stranger", price=100, stack=1)
order9 = orders.create_order(item9, 50, created_by="u:owner")
orders.claim(order9, "u:9", 50)
orders.mark_fulfilled(order9, "u:9", 50)
result9 = orders.approve(order9, "u:approver")
check("a Recruit worker is paid the bare payout amount, no bonus added",
      result9["paid_coins"] == charge(50, orders.worker_payout_for(100), 1),
      str(result9))

# ------------------------------------------------------------------ [6] the bet cap bonus is REAL
print("\nwagering.check_wager() actually raises MAX_BET/MAX_DAILY_LOSS for a ranked subject")
reset()
money.mint("u:10", 1_000_000, service="owner", reason="seed")
age_wallet("u:10")
loyalty.set_override("u:10", "worker", actor="u:owner")         # +10% on both caps
over_base = wagering.MAX_BET + 1
with db.db() as c:
    raised = False
    try:
        wagering.check_wager(c, "u:10", over_base, kind="games")
        raised = True
    except wagering.BetTooLarge:
        pass
check("a ranked subject can wager MORE than the bare MAX_BET without refusal",
      raised, "check_wager refused an amount within the raised cap")
with db.db() as c:
    raises_over_raised = False
    try:
        wagering.check_wager(c, "u:10", wagering.MAX_BET + (wagering.MAX_BET * 10) // 100 + 1,
                              kind="games")
    except wagering.BetTooLarge:
        raises_over_raised = True
check("but still refuses something past THEIR raised cap",
      raises_over_raised)

# A fresh, unranked wallet must see EXACTLY the base MAX_BET -- this is
# what every existing test in test_betting.py silently assumes.
reset()
money.mint("u:11", 1_000_000, service="owner", reason="seed")
age_wallet("u:11")
with db.db() as c:
    ok_at_base = False
    try:
        wagering.check_wager(c, "u:11", wagering.MAX_BET, kind="games")
        ok_at_base = True
    except wagering.BetTooLarge:
        pass
check("a Recruit (no rank history) is accepted at exactly the bare MAX_BET",
      ok_at_base)
with db.db() as c:
    refused_over_base = False
    try:
        wagering.check_wager(c, "u:11", wagering.MAX_BET + 1, kind="games")
    except wagering.BetTooLarge:
        refused_over_base = True
check("a Recruit is refused exactly 1 over the bare MAX_BET, unchanged behaviour",
      refused_over_base)


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all loyalty tests pass")
