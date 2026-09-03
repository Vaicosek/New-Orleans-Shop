"""The 2026-09-03 set: bounties, deadlines, team claims, reliability, stall
rent, and the monthly standings window.

Pins:
  [1] sweep_stale bumps an UNCLAIMED open order after BOUNTY_AFTER_DAYS, by
      BOUNTY_STEP_PCT of the SELL price, and never touches a claimed one.
  [2] the bump is idempotent per period -- two sweeps in a minute bump once.
  [3] the bounty stops at BOUNTY_CAP_PCT of the sale, however many sweeps.
      (The first version measured room from the base rate alone and climbed
      to 105% of the sale -- the shop paying to be rid of its own order.)
  [4] an open, unclaimed order past wanted_by is cancelled; a CLAIMED one
      past its date is somebody's work and is left alone.
  [5] claim(for_team=True) is refused for anyone who runs no team, and
      records team_id for a manager.
  [6] reliability counts finished orders only, and no record reads as 100.
  [7] sweep_rent charges once per period, skips the deposit month, and
      vacates a tenant who cannot cover it instead of running them negative.
  [8] leaderboard(since=...) windows money but never a team's existence.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-bounty-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import catalog, db, land, money, orders, teams                  # noqa: E402

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


def age(order_id: int, days: int) -> None:
    with db.db() as c:
        c.execute("UPDATE orders SET created_at = datetime('now', ?), bounty_at = NULL WHERE id = ?",
                  (f"-{days} days", order_id))


db.init_db()
money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")
money.mint("treasury:shop", 1_000_000, service="owner", reason="seed")
item = catalog.add_item("Oak Log", 320, price_unit_pieces=64, stack_size=64, category="Wood")
BASE = orders.worker_payout_for(320)

# ------------------------------------------------------------------ [1] bump unclaimed only
print("\nbounty: unclaimed work gets a step, claimed work does not")
stale = orders.create_order(item, 64, created_by="u:c")
busy = orders.create_order(item, 64, created_by="u:c"); orders.claim(busy, "u:w", 64)
age(stale, orders.BOUNTY_AFTER_DAYS + 1); age(busy, orders.BOUNTY_AFTER_DAYS + 1)
r = orders.sweep_stale()
check("the unclaimed order was bumped", stale in r["bumped"], str(r))
check("the claimed order was not", busy not in r["bumped"])
o = orders.get_order(stale)
check("bump is BOUNTY_STEP_PCT of the SELL price, not of the payout",
      o["payout_coins"] == BASE + (320 * orders.BOUNTY_STEP_PCT) // 100, str(dict(o)))
check("bounty_pct records the step", o["bounty_pct"] == orders.BOUNTY_STEP_PCT)
check("a fresh order is not bumped", orders.create_order(item, 64, created_by="u:c")
      not in orders.sweep_stale()["bumped"])

# ------------------------------------------------------------------ [2] idempotent per period
print("\nbounty: once per period")
r2 = orders.sweep_stale()
check("a second sweep in the same period bumps nothing", stale not in r2["bumped"], str(r2))

# ------------------------------------------------------------------ [3] the cap
print("\nbounty: capped at BOUNTY_CAP_PCT of the sale")
for _ in range(12):
    with db.db() as c:
        c.execute("UPDATE orders SET bounty_at = datetime('now','-30 days') WHERE id = ?", (stale,))
    orders.sweep_stale()
o = orders.get_order(stale)
check(f"payout never exceeds {orders.BOUNTY_CAP_PCT}% of the sell price",
      o["payout_coins"] * 100 // 320 <= orders.BOUNTY_CAP_PCT,
      f"payout {o['payout_coins']} = {o['payout_coins']*100//320}% of 320")
check("...and actually reaches the cap rather than stalling under it",
      o["payout_coins"] * 100 // 320 == orders.BOUNTY_CAP_PCT)

# ------------------------------------------------------------------ [4] deadlines
print("\ndeadline: open+unclaimed past wanted_by is dropped; claimed is kept")
late = orders.create_order(item, 64, created_by="u:c", wanted_in_days=1)
late_busy = orders.create_order(item, 64, created_by="u:c", wanted_in_days=1)
orders.claim(late_busy, "u:w2", 64)
with db.db() as c:
    c.execute("UPDATE orders SET wanted_by = datetime('now','-1 day') WHERE id IN (?, ?)", (late, late_busy))
r = orders.sweep_stale()
check("the unclaimed late order was cancelled",
      late in r["cancelled"] and orders.get_order(late)["status"] == "cancelled")
check("the claimed late order was left alone",
      late_busy not in r["cancelled"] and orders.get_order(late_busy)["status"] == "claimed")
raises("wanted_in_days must be positive", ValueError,
       orders.create_order, item, 64, "u:c", wanted_in_days=0)

# ------------------------------------------------------------------ [5] team claims
print("\nteam claim: managers only, recorded")
teams.create("u:mgr", "Crew"); teams.add_member("u:mgr", "u:member")
t_order = orders.create_order(item, 64, created_by="u:c")
raises("a non-manager cannot claim for a team", orders.NotClaimable,
       orders.claim, t_order, "u:member", 32, for_team=True)
cid = orders.claim(t_order, "u:mgr", 64, for_team=True)
with db.db() as c:
    row = c.execute("SELECT team_id, worker FROM order_claims WHERE id = ?", (cid,)).fetchone()
check("the manager is the worker of record", row["worker"] == "u:mgr")
check("the claim carries the team id", row["team_id"] is not None)

# ------------------------------------------------------------------ [6] reliability
print("\nreliability: finished orders only, no record reads as 100")
check("no finished claims -> 100%, 0 claims",
      orders.reliability("u:ghost") == {"claimed": 0, "delivered": 0, "claims": 0, "pct": 100})
good = orders.create_order(item, 64, created_by="u:c")
orders.claim(good, "u:r", 64); orders.mark_fulfilled(good, "u:r", 64); orders.approve(good, approver="u:staff")
half = orders.create_order(item, 64, created_by="u:c")
orders.claim(half, "u:r", 64); orders.mark_fulfilled(half, "u:r", 32); orders.cancel(half, actor="u:staff")
open_one = orders.create_order(item, 64, created_by="u:c"); orders.claim(open_one, "u:r", 64)
rel = orders.reliability("u:r")
check("128 claimed, 96 delivered, over the two FINISHED orders",
      rel["claimed"] == 128 and rel["delivered"] == 96 and rel["claims"] == 2, str(rel))
check("open work is not counted against them", rel["pct"] == 75)

# ------------------------------------------------------------------ [7] stalls
print("\nstalls: rent once a period, vacate rather than go negative")
money.mint("u:rich", 5_000, service="owner", reason="seed")
money.mint("u:poor", 1_050, service="owner", reason="seed")
s1 = land.open_listing("Stall 1", "", "", 500, 50, 60, buy_now_price=1_000, rent_coins=400, created_by="u:staff")
s2 = land.open_listing("Stall 2", "", "", 500, 50, 60, buy_now_price=1_000, rent_coins=400, created_by="u:staff")
land.bid(s1, "u:rich", 1_000); land.bid(s2, "u:poor", 1_000)
check("inside the deposit month nothing is charged", land.sweep_rent() == {"charged": [], "vacated": []})
with db.db() as c:
    c.execute("UPDATE land_listings SET settled_at = datetime('now','-31 days')")
t_before = money.balance("treasury:shop").coins
r = land.sweep_rent()
check("the solvent tenant was charged", s1 in r["charged"] and money.balance("u:rich").coins == 4_000 - 400)
check("the treasury received exactly the rent", money.balance("treasury:shop").coins == t_before + 400)
check("the tenant who could not cover it was vacated, not overdrawn",
      s2 in r["vacated"] and money.balance("u:poor").coins == 50)
check("a second sweep in the same period charges nothing", land.sweep_rent() == {"charged": [], "vacated": []})
with db.db() as c:
    check("exactly one rent row exists",
          c.execute("SELECT COUNT(*) FROM land_rent").fetchone()[0] == 1)
    check("the vacated stall is marked",
          c.execute("SELECT vacated_at FROM land_listings WHERE id = ?", (s2,)).fetchone()[0] is not None)
raises("negative rent is refused at listing time", land.LandError,
       land.open_listing, "Bad", "", "", 500, 50, 60, rent_coins=-1, created_by="u:staff")

# ------------------------------------------------------------------ [8] the window
print("\nstandings window: money is windowed, a team's existence is not")
old_order = orders.create_order(item, 64, created_by="u:c")
orders.claim(old_order, "u:member", 64); orders.mark_fulfilled(old_order, "u:member", 64)
orders.approve(old_order, approver="u:staff")
with db.db() as c:   # push that payout into last month
    c.execute("UPDATE orders SET closed_at = datetime('now','-40 days') WHERE id = ?", (old_order,))
    c.execute("UPDATE team_overrides SET paid_at = datetime('now','-40 days')")
all_time = {r["name"]: r for r in teams.leaderboard()}
month = {r["name"]: r for r in teams.leaderboard(since=teams.month_start())}
check("all-time board counts last month's work", all_time["Crew"]["worked"] > 0)
check("this-month board does not", month["Crew"]["worked"] == 0, str(month["Crew"]))
check("...but the team still appears on it", "Crew" in month)
check("month_start is the first of the month, midnight UTC",
      teams.month_start().endswith("-01 00:00:00"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all bounty/stall tests pass")
