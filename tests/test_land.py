"""Land -- staff-listed plot sales: open-bid auctions with an optional
instant buy-now price.

Same style as test_auctions.py, same infrastructure, most pins mirrored
1:1 since core/land.py mirrors core/auctions.py's lifecycle deliberately.
Pins:

  [1] full lifecycle: open -> bid -> outbid releases the previous leader's
      hold -> close -> settle pays treasury:shop and marks the winning bid
      'won'.
  [2] min_bid / min_increment floor enforcement.
  [3] orders_blocked refuses a bid explicitly.
  [4] a bid after closes_at is refused even before any sweep runs.
  [5] sweep_expired closes+settles every due listing, one bad row never
      blocking the rest.
  [6] void refunds the current leader in full and cannot be replayed.
  [7] settling with no bids leaves winner=None and moves no money.
  [8] settle_event replay safety.
  [9] buy-now: a bid that clears buy_now_price settles the listing
      INSTANTLY, in the same call -- no sweep needed, no window where the
      listing looks still-open after the winning bid lands. A bid below
      buy_now_price leaves the listing open and bid-only, same as an
      ordinary auction.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-land-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import db, land, money                              # noqa: E402

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
        for t in ("land_bids", "land_listings", "ledger_entries",
                  "ledger_holds", "wallet_flags", "idempotency", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")


def make_listing(name: str, *, min_bid: int = 100, min_increment: int = 50,
                  buy_now_price: int | None = None, duration_minutes: int = 30) -> int:
    return land.open_listing(name, "a fine plot", "spawn +0/+0", min_bid, min_increment,
                              duration_minutes, buy_now_price=buy_now_price,
                              created_by="u:staff")


db.init_db()

# ------------------------------------------------------------------ [1] full lifecycle
print("\nfull lifecycle: open -> bid -> outbid releases -> close -> settle pays treasury:shop")
reset()
money.mint("u:1", 10_000, service="owner", reason="seed")
money.mint("u:2", 10_000, service="owner", reason="seed")
land_id = make_listing("Riverside Lot 1")

bid1 = land.bid(land_id, "u:1", 100)
check("first bid at min_bid is accepted", isinstance(bid1["bid_id"], int))
check("first bid did not trigger buy-now (there is none)", bid1["bought_now"] is False)
check("u:1's balance shows the hold", money.balance("u:1").held == 100)

bid2 = land.bid(land_id, "u:2", 150)
check("second bid at exactly leader+min_increment is accepted", isinstance(bid2["bid_id"], int))
check("u:1's hold was released when outbid", money.balance("u:1").held == 0)
check("u:1's coins are untouched (never actually lost anything)", money.balance("u:1").coins == 10_000)
check("u:2's balance shows the new hold", money.balance("u:2").held == 150)

with db.db() as c:
    row1 = c.execute("SELECT status FROM land_bids WHERE id = ?", (bid1["bid_id"],)).fetchone()
check("the outbid bid is marked 'outbid'", row1["status"] == "outbid")

land.close(land_id)
result = land.settle(land_id, money.new_event_id("land.settle"))
check("settle reports the right winner", result["winner"] == "u:2")
check("settle reports the right winning amount", result["winning_amount"] == 150)
check("u:2's hold was captured (no longer held)", money.balance("u:2").held == 0)
check("u:2's coins dropped by the winning amount", money.balance("u:2").coins == 10_000 - 150)
check("treasury:shop received the proceeds", money.balance("treasury:shop").coins == 150)
with db.db() as c:
    row2 = c.execute("SELECT status FROM land_bids WHERE id = ?", (bid2["bid_id"],)).fetchone()
check("the winning bid is marked 'won'", row2["status"] == "won")

# ------------------------------------------------------------------ [2] floor enforcement
print("\nmin_bid / min_increment floor enforcement")
reset()
money.mint("u:3", 10_000, service="owner", reason="seed")
l2 = make_listing("Riverside Lot 2", min_bid=200, min_increment=25)
raises("a bid below min_bid is refused", land.BidTooLow, land.bid, l2, "u:3", 199)
check("the refused bid left no hold behind", money.balance("u:3").held == 0)
land.bid(l2, "u:3", 200)
money.mint("u:4", 10_000, service="owner", reason="seed")
raises("a raise smaller than min_increment is refused",
       land.BidTooLow, land.bid, l2, "u:4", 210)
land.bid(l2, "u:4", 225)
check("a raise exactly at the increment succeeds", money.balance("u:4").held == 225)

# ------------------------------------------------------------------ [3] orders_blocked
print("\norders_blocked refuses a bid explicitly")
reset()
money.mint("u:5", 10_000, service="owner", reason="seed")
l3 = make_listing("Riverside Lot 3", min_bid=50, min_increment=10)
money.set_flag("u:5", "orders_blocked", service="owner", set_by="owner")
raises("a blocked subject cannot bid", land.OrdersBlocked, land.bid, l3, "u:5", 50)
check("the refused bid left no hold behind", money.balance("u:5").held == 0)
money.clear_flag("u:5", "orders_blocked", service="owner")
bid_ok = land.bid(l3, "u:5", 50)
check("clearing the flag lets the bid through", isinstance(bid_ok["bid_id"], int))

# ------------------------------------------------------------------ [4] bid after closes_at
print("\na bid after closes_at is refused even before any sweep runs")
reset()
money.mint("u:6", 10_000, service="owner", reason="seed")
l4 = make_listing("Riverside Lot 4", min_bid=50, min_increment=10)
with db.db() as c:
    c.execute("UPDATE land_listings SET closes_at = datetime('now', '-1 minute') WHERE id = ?", (l4,))
raises("a bid past closes_at is refused, sweep or not",
       land.ListingNotOpen, land.bid, l4, "u:6", 50)
check("the refused bid left no hold behind", money.balance("u:6").held == 0)

# ------------------------------------------------------------------ [5] sweep_expired
print("\nsweep_expired closes+settles every due listing; one bad row never blocks the rest")
reset()
money.mint("u:7", 10_000, service="owner", reason="seed")
money.mint("u:8", 10_000, service="owner", reason="seed")
l5a = make_listing("Riverside Lot 5a", min_bid=50, min_increment=10)
l5b = make_listing("Riverside Lot 5b", min_bid=50, min_increment=10)
land.bid(l5a, "u:7", 50)
land.bid(l5b, "u:8", 75)
with db.db() as c:
    c.execute("UPDATE land_listings SET closes_at = datetime('now', '-1 minute') WHERE id IN (?, ?)",
              (l5a, l5b))
# l5a already 'closed' (not 'open') so sweep's close() call fails on it and
# must still go on to settle l5b.
land.close(l5a)
settled = land.sweep_expired()
check("sweep settles the listing that could still be closed", l5b in settled)
check("sweep does not silently crash on the already-closed one", isinstance(settled, list))
with db.db() as c:
    status_a = c.execute("SELECT status FROM land_listings WHERE id = ?", (l5a,)).fetchone()["status"]
    status_b = c.execute("SELECT status FROM land_listings WHERE id = ?", (l5b,)).fetchone()["status"]
check("the already-closed listing is still settleable on a later sweep",
      status_a == "closed", status_a)
check("the good listing settled", status_b == "settled", status_b)
check("treasury:shop only received the good listing's proceeds",
      money.balance("treasury:shop").coins == 75)

result5a = land.settle(l5a, money.new_event_id("land.settle"))
check("the straggler settles cleanly once closed", result5a["winner"] == "u:7")

# ------------------------------------------------------------------ [6] void refunds in full
print("\nvoid refunds the current leader in full and cannot be replayed")
reset()
money.mint("u:9", 10_000, service="owner", reason="seed")
l6 = make_listing("Riverside Lot 6", min_bid=50, min_increment=10)
land.bid(l6, "u:9", 50)
check("the bid holds the coins before void", money.balance("u:9").held == 50)
voided = land.void(l6, actor="u:staff")
check("void reports it actually did something", voided is True)
check("the leader's hold is fully released", money.balance("u:9").held == 0)
check("the leader kept every coin", money.balance("u:9").coins == 10_000)
with db.db() as c:
    row6 = c.execute("SELECT status FROM land_bids WHERE land_id = ?", (l6,)).fetchone()
check("the refunded bid is marked 'refunded'", row6["status"] == "refunded")
voided_again = land.void(l6, actor="u:staff")
check("voiding an already-voided listing is a no-op, not an error", voided_again is False)
raises("bidding on a voided listing is refused",
       land.ListingNotOpen, land.bid, l6, "u:9", 100)

# ------------------------------------------------------------------ [7] settle with no bids
print("\nsettling with no bids leaves winner=None and moves no money")
reset()
l7 = make_listing("Riverside Lot 7", min_bid=50, min_increment=10)
land.close(l7)
result7 = land.settle(l7, money.new_event_id("land.settle"))
check("no-bid settlement reports no winner", result7["winner"] is None)
check("no-bid settlement reports no winning amount", result7["winning_amount"] is None)
check("no-bid settlement moved nothing into treasury:shop",
      money.balance("treasury:shop").coins == 0)

# ------------------------------------------------------------------ [8] settle_event replay safety
print("\nsettle_event replay safety: same event id replays, different event id is refused")
reset()
money.mint("u:10", 10_000, service="owner", reason="seed")
l8 = make_listing("Riverside Lot 8", min_bid=50, min_increment=10)
land.bid(l8, "u:10", 50)
land.close(l8)
event_id = money.new_event_id("land.settle")
first = land.settle(l8, event_id)
replay = land.settle(l8, event_id)
check("replaying the same event id returns the same summary", replay == first)
check("the replay did not move money a second time",
      money.balance("treasury:shop").coins == 50)
raises("settling an already-settled listing with a DIFFERENT event id is refused",
       land.AlreadySettled, land.settle, l8, money.new_event_id("land.settle"))

# ------------------------------------------------------------------ [9] buy-now
print("\nbuy-now: a bid clearing buy_now_price settles instantly; a lower bid does not")
reset()
money.mint("u:11", 10_000, service="owner", reason="seed")
money.mint("u:12", 10_000, service="owner", reason="seed")
l9 = make_listing("Riverside Lot 9", min_bid=100, min_increment=50, buy_now_price=1_000)

under = land.bid(l9, "u:11", 500)
check("a bid below buy_now_price does not trigger it", under["bought_now"] is False)
check("the settlement field is empty for a non-buy-now bid", under["settlement"] is None)
with db.db() as c:
    still_open = c.execute("SELECT status FROM land_listings WHERE id = ?", (l9,)).fetchone()["status"]
check("the listing is still open after a sub-buy-now bid", still_open == "open", still_open)

over = land.bid(l9, "u:12", 1_000)
check("a bid meeting buy_now_price triggers it", over["bought_now"] is True)
check("buy-now returns a settlement summary in the same call", over["settlement"] is not None)
check("the settlement's winner is the buy-now bidder", over["settlement"]["winner"] == "u:12")
with db.db() as c:
    sold = c.execute("SELECT status FROM land_listings WHERE id = ?", (l9,)).fetchone()["status"]
check("the listing is 'settled' immediately, no sweep needed", sold == "settled", sold)
check("u:11's earlier hold was released when outbid by the buy-now bid",
      money.balance("u:11").held == 0)
check("u:11 lost nothing", money.balance("u:11").coins == 10_000)
check("u:12's hold was captured, not left open", money.balance("u:12").held == 0)
check("u:12 paid exactly the buy-now price", money.balance("u:12").coins == 10_000 - 1_000)
check("treasury:shop received the buy-now proceeds", money.balance("treasury:shop").coins == 1_000)
raises("bidding after a buy-now sale is refused, same as any settled listing",
       land.ListingNotOpen, land.bid, l9, "u:11", 1_500)

l9b = make_listing("Riverside Lot 9b", min_bid=100, min_increment=50, buy_now_price=1_000)
over_bid = land.bid(l9b, "u:11", 1_200)  # clears buy_now with room to spare
check("a bid ABOVE buy_now_price also triggers it (>=, not ==)", over_bid["bought_now"] is True)
check("the buyer pays their actual bid, not a clamped buy_now_price",
      over_bid["settlement"]["winning_amount"] == 1_200)

raises("buy_now_price below min_bid is refused at listing time",
       land.LandError, land.open_listing, "Bad Lot", "", "", 500, 50, 30,
       buy_now_price=100, created_by="u:staff")


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all land tests pass")
