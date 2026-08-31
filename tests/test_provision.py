"""The `/setup` layout planner.

The case that matters is the SECOND run. Someone will run `/setup` twice, and
the second run must be boring -- no duplicate #shop, no second Staff role.
Discord allows same-named channels, so a planner that only checked "did I
build this" and not "does it still exist" produces a guild nobody can navigate
and a picker with two identical entries.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NOLA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

from core import provision  # noqa: E402
from core.db import init_db  # noqa: E402

init_db()

GUILD = 999
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")
    if not ok:
        FAILS.append(name)


def actions(steps):
    return {s.key: s.action for s in steps}


print("a fresh server: everything is created")

steps = provision.plan(GUILD, live_ids=[], existing_by_name={}, stored_ids={})
acts = actions(steps)
check("every desired item is a create", set(acts.values()) == {"create"}, str(set(acts.values())))
check("the plan covers the whole layout", len(steps) == len(provision.DESIRED))
check("summary counts the creates", provision.summarise(steps)["create"] == len(provision.DESIRED))

print("\ncategories are planned before the channels that sit in them")

order = [s.key for s in steps]
misordered = [
    s.desired.parent for s in steps
    if s.desired.parent and order.index(s.desired.parent) > order.index(s.key)
]
check("no channel is planned before its parent category", not misordered,
      f"parents planned too late: {misordered}")

print("\na hand-made channel with the same name is ADOPTED, never duplicated")

steps = provision.plan(
    GUILD,
    live_ids=[],
    existing_by_name={("channel", "shop"): 4001, ("role", "staff"): 4002},
    stored_ids={},
)
acts = actions(steps)
check("an existing #shop is adopted", acts["channel:shop"] == "adopt")
check("adoption carries the existing id",
      next(s for s in steps if s.key == "channel:shop").existing_id == 4001)
check("an existing Staff role is adopted", acts["role:staff"] == "adopt")
check("everything else is still created", acts["channel:casino"] == "create")

print("\nthe second run is boring: what we built and still exists is left alone")

for step in provision.plan(GUILD, live_ids=[], existing_by_name={}, stored_ids={}):
    provision.record(GUILD, step.key, 5000 + len(step.key), step.desired.name)

live = list(provision.stored(GUILD).values())
steps = provision.plan(GUILD, live_ids=live, existing_by_name={})
check("a second run creates nothing", provision.summarise(steps)["create"] == 0,
      str(provision.summarise(steps)))
check("a second run adopts nothing either", provision.summarise(steps)["adopt"] == 0)
check("every item reports ok", set(actions(steps).values()) == {"ok"})

print("\nA STORED ID POINTING AT A DELETED CHANNEL IS NOT 'ok'")

dead = provision.channel_id(GUILD, "channel:alerts")
live_without_alerts = [i for i in live if i != dead]
steps = provision.plan(GUILD, live_ids=live_without_alerts, existing_by_name={})
check("a deleted channel is rebuilt, not reported as fine",
      actions(steps)["channel:alerts"] == "create",
      "a row pointing at a dead channel looks configured right up until an alert goes nowhere")
check("the surviving items are untouched", actions(steps)["channel:shop"] == "ok")

check("is_complete() is False while anything is missing",
      provision.is_complete(GUILD, live_without_alerts) is False)
check("is_complete() is True when every id resolves",
      provision.is_complete(GUILD, live) is True)

print("\nre-recording a key repoints it instead of duplicating")

provision.record(GUILD, "channel:alerts", 7777, "alerts")
check("last write wins", provision.channel_id(GUILD, "channel:alerts") == 7777)
rows = [k for k in provision.stored(GUILD) if k == "channel:alerts"]
check("exactly one row per key", len(rows) == 1)

print("\nforget() clears a stale mapping")

provision.forget(GUILD, "channel:alerts")
check("a forgotten key is gone", provision.channel_id(GUILD, "channel:alerts") is None)

print("\nthe three formerly-required env vars all have a home in the layout")

env_backed = {d.env_name for d in provision.DESIRED if d.env_name}
check("SHOP, ORDERS and ALERTS channels are all provisioned",
      env_backed == {"SHOP_CHANNEL_ID", "ORDERS_CHANNEL_ID", "ALERTS_CHANNEL_ID"},
      str(env_backed))

print("\nstaff-only things are marked as such")

staff_keys = {d.key for d in provision.DESIRED if d.staff_only}
check("alerts and the audit log are staff-only",
      {"channel:alerts", "channel:audit-log", "category:staff"} <= staff_keys, str(staff_keys))
check("the shop is NOT staff-only", "channel:shop" not in staff_keys)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all provision tests pass")
