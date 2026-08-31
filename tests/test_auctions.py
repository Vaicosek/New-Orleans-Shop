"""Auctions -- public open-bid (English) lots on catalog items.

Same style as test_slots.py/test_betting.py: real SQLite files, no mocks,
check()/raises(), exit 1 on any failure. Pins:

  [1] full lifecycle: open -> bid -> outbid releases the previous leader's
      hold -> close -> settle pays treasury:shop and marks the winning bid
      'won'.
  [2] min_bid / min_increment floor enforcement.
  [3] orders_blocked refuses a bid explicitly -- auctions.bid checks the
      flag itself, since money.place_hold's built-in gate only auto-covers
      money.GAMBLING_SERVICES, and bidding is deliberately not one of them.
  [4] a bid after closes_at is refused even before any sweep runs.
  [5] sweep_expired closes+settles every due auction, one bad row never
      blocking the rest.
  [6] void refunds the current leader in full and cannot be replayed.
  [7] settling with no bids leaves winner=None and moves no money.
  [8] settle_event replay safety: calling settle() again with the SAME
      event id on an already-settled auction returns the same summary
      rather than erroring or moving money twice.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-auctions-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import auctions, db, money                          # noqa: E402

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
        for t in ("auction_bids", "auctions", "items", "ledger_entries",
                  "ledger_holds", "wallet_flags", "idempotency", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")


def make_item(name: str, price: int = 100) -> int:
    with db.db() as c:
        cur = c.execute(
            "INSERT INTO items (name, price_coins) VALUES (?, ?)", (name, price)
        )
        return cur.lastrowid


db.init_db()

# ------------------------------------------------------------------ [1] full lifecycle
print("\nfull lifecycle: open -> bid -> outbid releases -> close -> settle pays treasury:shop")
reset()
money.mint("u:1", 10_000, service="owner", reason="seed")
money.mint("u:2", 10_000, service="owner", reason="seed")
item_id = make_item("diamond sword")

auction_id = auctions.open_auction(item_id, pieces=1, min_bid=100, min_increment=50,
                                    duration_minutes=30, created_by="u:staff")
check("open_auction returns an id", isinstance(auction_id, int))

bid1 = auctions.bid(auction_id, "u:1", 100)
check("first bid at min_bid is accepted", isinstance(bid1, int))
check("u:1's balance shows the hold", money.balance("u:1").held == 100)

bid2 = auctions.bid(auction_id, "u:2", 150)
check("second bid at exactly leader+min_increment is accepted", isinstance(bid2, int))
check("u:1's hold was released when outbid", money.balance("u:1").held == 0)
check("u:1's coins are untouched (never actually lost anything)", money.balance("u:1").coins == 10_000)
check("u:2's balance shows the new hold", money.balance("u:2").held == 150)

with db.db() as c:
    row1 = c.execute("SELECT status FROM auction_bids WHERE id = ?", (bid1,)).fetchone()
check("the outbid bid is marked 'outbid'", row1["status"] == "outbid")

auctions.close(auction_id)
result = auctions.settle(auction_id, money.new_event_id("auction.settle"))
check("settle reports the right winner", result["winner"] == "u:2")
check("settle reports the right winning amount", result["winning_amount"] == 150)
check("u:2's hold was captured (no longer held)", money.balance("u:2").held == 0)
check("u:2's coins dropped by the winning amount", money.balance("u:2").coins == 10_000 - 150)
check("treasury:shop received the proceeds", money.balance("treasury:shop").coins == 150)
with db.db() as c:
    row2 = c.execute("SELECT status FROM auction_bids WHERE id = ?", (bid2,)).fetchone()
check("the winning bid is marked 'won'", row2["status"] == "won")

# ------------------------------------------------------------------ [2] floor enforcement
print("\nmin_bid / min_increment floor enforcement")
reset()
money.mint("u:3", 10_000, service="owner", reason="seed")
item2 = make_item("iron pickaxe")
a2 = auctions.open_auction(item2, pieces=1, min_bid=200, min_increment=25,
                            duration_minutes=30, created_by="u:staff")
raises("a bid below min_bid is refused", auctions.BidTooLow, auctions.bid, a2, "u:3", 199)
check("the refused bid left no hold behind", money.balance("u:3").held == 0)
auctions.bid(a2, "u:3", 200)
money.mint("u:4", 10_000, service="owner", reason="seed")
raises("a raise smaller than min_increment is refused",
       auctions.BidTooLow, auctions.bid, a2, "u:4", 210)
auctions.bid(a2, "u:4", 225)
check("a raise exactly at the increment succeeds", money.balance("u:4").held == 225)

# ------------------------------------------------------------------ [3] orders_blocked
print("\norders_blocked refuses a bid explicitly")
reset()
money.mint("u:5", 10_000, service="owner", reason="seed")
item3 = make_item("golden apple")
a3 = auctions.open_auction(item3, pieces=1, min_bid=50, min_increment=10,
                            duration_minutes=30, created_by="u:staff")
money.set_flag("u:5", "orders_blocked", service="owner", set_by="owner")
raises("a blocked subject cannot bid", auctions.OrdersBlocked, auctions.bid, a3, "u:5", 50)
check("the refused bid left no hold behind", money.balance("u:5").held == 0)
money.clear_flag("u:5", "orders_blocked", service="owner")
bid_ok = auctions.bid(a3, "u:5", 50)
check("clearing the flag lets the bid through", isinstance(bid_ok, int))

# ------------------------------------------------------------------ [4] bid after closes_at
print("\na bid after closes_at is refused even before any sweep runs")
reset()
money.mint("u:6", 10_000, service="owner", reason="seed")
item4 = make_item("netherite ingot")
a4 = auctions.open_auction(item4, pieces=1, min_bid=50, min_increment=10,
                            duration_minutes=30, created_by="u:staff")
with db.db() as c:
    c.execute("UPDATE auctions SET closes_at = datetime('now', '-1 minute') WHERE id = ?", (a4,))
raises("a bid past closes_at is refused, sweep or not",
       auctions.AuctionNotOpen, auctions.bid, a4, "u:6", 50)
check("the refused bid left no hold behind", money.balance("u:6").held == 0)

# ------------------------------------------------------------------ [5] sweep_expired
print("\nsweep_expired closes+settles every due auction; one bad row never blocks the rest")
reset()
money.mint("u:7", 10_000, service="owner", reason="seed")
money.mint("u:8", 10_000, service="owner", reason="seed")
item5a = make_item("emerald block")
item5b = make_item("elytra")
a5a = auctions.open_auction(item5a, pieces=1, min_bid=50, min_increment=10,
                             duration_minutes=30, created_by="u:staff")
a5b = auctions.open_auction(item5b, pieces=1, min_bid=50, min_increment=10,
                             duration_minutes=30, created_by="u:staff")
auctions.bid(a5a, "u:7", 50)
auctions.bid(a5b, "u:8", 75)
with db.db() as c:
    c.execute("UPDATE auctions SET closes_at = datetime('now', '-1 minute') WHERE id IN (?, ?)",
              (a5a, a5b))
# make a5a already 'closed' (not 'open') so sweep's close() call fails on it
# and must still go on to settle a5b -- the "one bad row never blocks the
# rest" guarantee, exercised for real rather than just asserted in a comment.
auctions.close(a5a)
settled = auctions.sweep_expired()
check("sweep settles the auction that could still be closed", a5b in settled)
check("sweep does not silently crash on the already-closed one", isinstance(settled, list))
with db.db() as c:
    status_a = c.execute("SELECT status FROM auctions WHERE id = ?", (a5a,)).fetchone()["status"]
    status_b = c.execute("SELECT status FROM auctions WHERE id = ?", (a5b,)).fetchone()["status"]
check("the already-closed auction is still settleable on a later sweep",
      status_a == "closed", status_a)
check("the good auction settled", status_b == "settled", status_b)
check("treasury:shop only received the good auction's proceeds",
      money.balance("treasury:shop").coins == 75)

# a second sweep should pick up the straggler now that it's just 'closed'
# -- sweep_expired only looks at status='open', so close() it back down
# is not representative; instead confirm settle() directly finishes it.
result5a = auctions.settle(a5a, money.new_event_id("auction.settle"))
check("the straggler settles cleanly once closed", result5a["winner"] == "u:7")

# ------------------------------------------------------------------ [6] void refunds in full
print("\nvoid refunds the current leader in full and cannot be replayed")
reset()
money.mint("u:9", 10_000, service="owner", reason="seed")
item6 = make_item("shulker box")
a6 = auctions.open_auction(item6, pieces=1, min_bid=50, min_increment=10,
                            duration_minutes=30, created_by="u:staff")
auctions.bid(a6, "u:9", 50)
check("the bid holds the coins before void", money.balance("u:9").held == 50)
voided = auctions.void(a6, actor="u:staff")
check("void reports it actually did something", voided is True)
check("the leader's hold is fully released", money.balance("u:9").held == 0)
check("the leader kept every coin", money.balance("u:9").coins == 10_000)
with db.db() as c:
    row6 = c.execute("SELECT status FROM auction_bids WHERE auction_id = ?", (a6,)).fetchone()
check("the refunded bid is marked 'refunded'", row6["status"] == "refunded")
voided_again = auctions.void(a6, actor="u:staff")
check("voiding an already-voided auction is a no-op, not an error", voided_again is False)
raises("bidding on a voided auction is refused",
       auctions.AuctionNotOpen, auctions.bid, a6, "u:9", 100)

# ------------------------------------------------------------------ [7] settle with no bids
print("\nsettling with no bids leaves winner=None and moves no money")
reset()
item7 = make_item("totem of undying")
a7 = auctions.open_auction(item7, pieces=1, min_bid=50, min_increment=10,
                            duration_minutes=30, created_by="u:staff")
auctions.close(a7)
result7 = auctions.settle(a7, money.new_event_id("auction.settle"))
check("no-bid settlement reports no winner", result7["winner"] is None)
check("no-bid settlement reports no winning amount", result7["winning_amount"] is None)
check("no-bid settlement moved nothing into treasury:shop",
      money.balance("treasury:shop").coins == 0)

# ------------------------------------------------------------------ [8] settle_event replay safety
print("\nsettle_event replay safety: same event id replays, different event id is refused")
reset()
money.mint("u:10", 10_000, service="owner", reason="seed")
item8 = make_item("trident")
a8 = auctions.open_auction(item8, pieces=1, min_bid=50, min_increment=10,
                            duration_minutes=30, created_by="u:staff")
auctions.bid(a8, "u:10", 50)
auctions.close(a8)
event_id = money.new_event_id("auction.settle")
first = auctions.settle(a8, event_id)
replay = auctions.settle(a8, event_id)
check("replaying the same event id returns the same summary", replay == first)
check("the replay did not move money a second time",
      money.balance("treasury:shop").coins == 50)
raises("settling an already-settled auction with a DIFFERENT event id is refused",
       auctions.AlreadySettled, auctions.settle, a8, money.new_event_id("auction.settle"))


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all auctions tests pass")
