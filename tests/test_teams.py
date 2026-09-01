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

from core import db, teams                                    # noqa: E402

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

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all teams tests pass")
