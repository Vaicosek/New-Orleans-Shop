"""Teams -- manager rosters. No money moves here (see CONTRACT.md 11d), so
there is no idempotency-replay pin to test; the pins are the invariants a
plain roster table still has to hold.

Pins:
  [1] create -> a manager gets a team; a second create for the same manager
      is refused (AlreadyHasTeam), never a silent second row.
  [2] add_member / remove_member: manager-only, and a manager can't add
      themselves.
  [3] join moves a member OFF whatever team they were already on -- one
      team per member, enforced by the UNIQUE column, not by application
      code remembering to check.
  [4] a manager can't join their own team as a member (create-path and
      join-path both).
  [5] leave: True when it actually removed a membership, False when there
      was nothing to leave.
  [6] disband deletes the team AND every membership row with it (cascade).
  [7] rename/add/remove all refuse a caller who isn't the actual manager of
      that team (NotYourTeam via the UnknownTeam path -- there's no row for
      a non-manager to match, which is the refusal that matters).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-teams-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import catalog, db, loyalty, money, orders, teams    # noqa: E402

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
        c.execute("DELETE FROM wallets")  # cascades teams + team_members


db.init_db()

# ------------------------------------------------------------------ [1] create / no duplicate
print("\ncreate: one team per manager")
reset()
team_id = teams.create("u:mgr1", "The Levee Crew")
check("create returns an int id", isinstance(team_id, int))
row = teams.team_of("u:mgr1")
check("team_of finds it by manager", row is not None and row["id"] == team_id)
check("name stored as given", row["name"] == "The Levee Crew")
raises("a second create for the same manager is refused",
       teams.AlreadyHasTeam, teams.create, "u:mgr1", "Second Team")

# ------------------------------------------------------------------ [2] add / remove, manager-only
print("\nadd_member / remove_member")
reset()
teams.create("u:mgr1", "Crew")
teams.add_member("u:mgr1", "u:w1")
teams.add_member("u:mgr1", "u:w2")
check("roster has both", set(teams.team_of("u:w1")
      and teams.roster(teams.team_of("u:w1")["id"])) == {"u:w1", "u:w2"})
raises("a manager can't add themselves",
       teams.TeamError, teams.add_member, "u:mgr1", "u:mgr1")
raises("add_member from someone who runs no team is refused",
       teams.UnknownTeam, teams.add_member, "u:nobody", "u:w3")
teams.remove_member("u:mgr1", "u:w1")
check("removed member is off the roster",
      teams.roster(teams.team_of("u:w2")["id"]) == ["u:w2"])

# ------------------------------------------------------------------ [3] join moves you, doesn't duplicate
print("\njoin: one team per member at a time")
reset()
t1 = teams.create("u:mgr1", "Crew One")
t2 = teams.create("u:mgr2", "Crew Two")
teams.join("u:w1", t1)
check("w1 is on team one", teams.team_of("u:w1")["id"] == t1)
teams.join("u:w1", t2)
check("joining team two moves w1, doesn't add a second row",
      teams.team_of("u:w1")["id"] == t2)
check("w1 no longer shows on team one's roster", "u:w1" not in teams.roster(t1))
check("w1 shows on team two's roster", "u:w1" in teams.roster(t2))

# ------------------------------------------------------------------ [4] manager can't join their own team
print("\na manager can't join their own team as a member")
raises("join() refuses the team's own manager",
       teams.TeamError, teams.join, "u:mgr2", t2)

# ------------------------------------------------------------------ [5] leave: True vs False
print("\nleave reports whether it actually removed something")
check("leave returns True for a real member", teams.leave("u:w1") is True)
check("leave returns False for someone on no team", teams.leave("u:w1") is False)
check("leave returns False for someone who never joined anything",
      teams.leave("u:ghost") is False)

# ------------------------------------------------------------------ [6] disband cascades
print("\ndisband deletes the team and its memberships")
reset()
t3 = teams.create("u:mgr3", "Crew Three")
teams.add_member("u:mgr3", "u:w9")
teams.disband("u:mgr3")
check("team_of no longer finds the manager's team", teams.team_of("u:mgr3") is None)
check("the former member is off any team too", teams.team_of("u:w9") is None)
raises("disband again is refused -- there's no team left to disband",
       teams.UnknownTeam, teams.disband, "u:mgr3")

# ------------------------------------------------------------------ [7] only the real manager can touch a team
print("\nrename/add/remove refuse a non-manager caller")
reset()
teams.create("u:mgr1", "Crew")
raises("rename from a non-manager subject is refused",
       teams.UnknownTeam, teams.rename, "u:not-the-manager", "New Name")
raises("remove_member from a non-manager subject is refused",
       teams.UnknownTeam, teams.remove_member, "u:not-the-manager", "u:w1")

# ------------------------------------------------------------------ [8] focus: absence means ALL
print("\nfocus: no rows means every category, not none")
reset()
with db.db() as c:
    c.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES ('Wood', 1)")
    c.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES ('Ores', 2)")
ta = teams.create("u:mgrA", "Wood crew"); teams.add_member("u:mgrA", "u:wa")
tb = teams.create("u:mgrB", "Ore crew");  teams.add_member("u:mgrB", "u:wb")
tc = teams.create("u:mgrC", "Empty crew")   # no members -- never pinged

check("a team with no focus works everything", teams.focus(ta) == [])
check("an unfocused team is offered every category",
      {t["name"] for t in teams.teams_for_category("Wood")} == {"Wood crew", "Ore crew"})

teams.set_focus("u:mgrA", ["Wood"])
teams.set_focus("u:mgrB", ["Ores"])
check("focus stored", teams.focus(ta) == ["Wood"] and teams.focus(tb) == ["Ores"])
check("a Wood order reaches only the wood crew",
      [t["name"] for t in teams.teams_for_category("Wood")] == ["Wood crew"])
check("an Ores order reaches only the ore crew",
      [t["name"] for t in teams.teams_for_category("Ores")] == ["Ore crew"])
check("a team with no members is never offered work",
      all(t["name"] != "Empty crew" for t in teams.teams_for_category("Wood")))

# The multi-select bug: a set that can only ever grow.
teams.set_focus("u:mgrB", ["Ores", "Wood"])
check("focus can be widened", teams.focus(tb) == ["Ores", "Wood"])
teams.set_focus("u:mgrB", ["Ores"])
check("focus can be NARROWED again -- set_focus replaces, never merges",
      teams.focus(tb) == ["Ores"])
teams.set_focus("u:mgrB", [])
check("clearing focus means everything again, not nothing",
      teams.focus(tb) == [] and
      {t["name"] for t in teams.teams_for_category("Wood")} == {"Wood crew", "Ore crew"})
check("set_focus de-duplicates", (teams.set_focus("u:mgrA", ["Wood", "Wood"]) or
                                   teams.focus(ta)) == ["Wood"])
raises("set_focus from a non-manager is refused",
       teams.UnknownTeam, teams.set_focus, "u:nobody", ["Wood"])

# ------------------------------------------------------------------ [9] leaderboard is derived, not stored
print("\nleaderboard: derived live from what was actually PAID")
reset()
with db.db() as c:
    c.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES ('Wood', 1)")
money.ensure_wallet("treasury:shop", deficit_floor=0, service="owner")
money.mint("treasury:shop", 1_000_000, service="owner", reason="seed")
item = catalog.add_item("Oak Log", 32, price_unit_pieces=64, stack_size=64, category="Wood")
teams.create("u:m1", "Alpha"); teams.add_member("u:m1", "u:x1")
teams.create("u:m2", "Beta");  teams.add_member("u:m2", "u:x2")

def _paid_order(worker, pieces):
    oid = orders.create_order(item, pieces, created_by="u:cust")
    orders.claim(oid, worker, pieces)
    orders.mark_fulfilled(oid, worker, pieces)
    orders.approve(oid, approver="u:staff")

# Figures come from the PAYOUT rate, not the sell price: the shop sells at
# `price_coins` and pays workers at `payout_coins`, and the leaderboard
# measures money that actually reached people (CONTRACT.md 11d).
from core.pricing import charge as _charge                       # noqa: E402
_ALPHA = _charge(64, orders.worker_payout_for(32), 64)
_BETA = _charge(256, orders.worker_payout_for(32), 64)

_paid_order("u:x1", 64)      # Alpha member
_paid_order("u:m2", 256)     # Beta MANAGER's own claim

board = {r["name"]: r for r in teams.leaderboard()}
check("a manager's own claims count toward their team",
      board["Beta"]["paid"] == _BETA and board["Beta"]["orders"] == 1,
      f"got {board['Beta']}, wanted {_BETA}")
# A standing is two things: gold paid for work the team's people delivered,
# and override earned by running the team. Both are reported separately so a
# team that produces nothing and rides on override is visibly different.
_ALPHA_CUT = (_ALPHA * teams.MANAGER_OVERRIDE_PCT) // 100
check("a member's claims count as WORKED", board["Alpha"]["worked"] == _ALPHA,
      f"got {board['Alpha']}, wanted {_ALPHA}")
check("the manager's override on that work counts as MANAGED",
      board["Alpha"]["managed"] == _ALPHA_CUT,
      f"got {board['Alpha']['managed']}, wanted {_ALPHA_CUT}")
check("the standing is the two added together",
      board["Alpha"]["paid"] == _ALPHA + _ALPHA_CUT)
check("managing earns loyalty points too, not just coins",
      loyalty.earned_points("u:m1") >= _ALPHA_CUT // loyalty.POINTS_DIVISOR)
check("ranked by gold paid, highest first",
      [r["name"] for r in teams.leaderboard()][:2] == ["Beta", "Alpha"])

# Delivered but unapproved is not money and must not rank.
pending = orders.create_order(item, 4096, created_by="u:cust")
orders.claim(pending, "u:x1", 4096)
orders.mark_fulfilled(pending, "u:x1", 4096)
after = {r["name"]: r for r in teams.leaderboard()}
check("delivered-but-unapproved work does NOT rank",
      after["Alpha"]["worked"] == _ALPHA and after["Alpha"]["orders"] == 1,
      f"got {after['Alpha']}")
check("...and it earns the manager no override either",
      after["Alpha"]["managed"] == _ALPHA_CUT, f"got {after['Alpha']['managed']}")
check("...and the ranking is unchanged by it",
      [r["name"] for r in teams.leaderboard()][:2] == ["Beta", "Alpha"])

# ------------------------------------------------------------------ [10] the margin is settable, and safely
print("\nthe worker share is configurable, and a bad value never becomes a payment")
# No reset() here on purpose: this section touches only `config`, and
# wiping wallets after money has moved trips ledger_entries' foreign key --
# the ledger is append-only and deliberately does NOT cascade.
check("defaults to the constant when nothing is stored",
      orders.payout_pct() == orders.WORKER_PAYOUT_PCT)
db.set_config(orders.PAYOUT_PCT_KEY, "60")
check("a stored value wins", orders.payout_pct() == 60)
check("...and it actually changes what an order will pay",
      orders.worker_payout_for(320) == 192)
for _bad in ("0", "-5", "500", "abc", ""):
    db.set_config(orders.PAYOUT_PCT_KEY, _bad)
    check(f"a stored {_bad!r} falls back to the default, never to a payment",
          orders.payout_pct() == orders.WORKER_PAYOUT_PCT,
          f"got {orders.payout_pct()}")
db.set_config(orders.PAYOUT_PCT_KEY, str(orders.WORKER_PAYOUT_PCT))
check("worker_payout_for never returns 0 for a priced item (0 strands delivered work)",
      all(orders.worker_payout_for(p) >= 1 for p in range(1, 50)))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all teams tests pass")
