"""The reference-market mirror. Runs its checks at import and exits non-zero.

The payload below is a REAL response captured from the source, trimmed of its
`trend_7d` arrays. That matters: a fixture invented to match the parser proves
only that the parser matches itself. If they change a field name, this file is
where it shows up, and it shows up as a failing test rather than as a column of
em-dashes that nobody questions for a month.

No network is touched here. `pull()` is the only function that reaches out and
it is exercised by the live boot log, not by the suite -- a test suite that
depends on somebody else's server going down is a test suite that lies.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NOLA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "refmarket.db")

from core import catalog, db, refmarket  # noqa: E402

db.init_db()

PAYLOAD = [
    {"item_key": "minecraft:smooth_stone", "material": "SMOOTH_STONE",
     "display_name": "Smooth Stone", "market_price": 0.01566711590296496,
     "market_price_source": "trades_24h", "best_ask_price": 1, "best_ask_amount": 64,
     "best_ask_unit_price": 0.015625, "best_bid_price": None, "best_bid_amount": None,
     "best_bid_unit_price": None, "spread_pct": None, "total_stock": 313,
     "total_demand": 0, "volume_24h": 5936, "last_updated_at": 1788184415},
    {"item_key": "minecraft:spruce_log", "material": "SPRUCE_LOG",
     "display_name": "Spruce Log", "market_price": 0.015625,
     "market_price_source": "trades_24h", "best_ask_price": 1, "best_ask_amount": 64,
     "best_ask_unit_price": 0.015625, "best_bid_price": 1, "best_bid_amount": 64,
     "best_bid_unit_price": 0.015625, "spread_pct": 0, "total_stock": 283,
     "total_demand": 107, "volume_24h": 5504, "last_updated_at": 1788184415},
]

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if not condition:
        failures.append(label)


# --- normalisation ---------------------------------------------------------
# The one rule that lines their catalogue up with ours. All four spellings of
# the same block have to land on the same string or the join silently misses.
check("match_name collapses their key",
      refmarket.match_name("minecraft:smooth_stone") == "smoothstone")
check("match_name collapses their material",
      refmarket.match_name("SMOOTH_STONE") == "smoothstone")
check("match_name collapses our name",
      refmarket.match_name("Smooth Stone") == "smoothstone")
check("match_name is not fooled by punctuation",
      refmarket.match_name("Jack o'Lantern") == refmarket.match_name("JACK_O_LANTERN"))

# --- storing a real payload ------------------------------------------------
stored = refmarket.store(PAYLOAD)
check("both rows stored", stored == 2)

with db.db() as c:
    row = c.execute("SELECT * FROM ref_market WHERE item_key = 'minecraft:spruce_log'").fetchone()
check("price read off the real field name", row is not None and row["price"] == 0.015625)
check("demand read off the real field name", row is not None and row["demand"] == 107)
check("stock read off the real field name", row is not None and row["stock"] == 283)
check("bid uses the UNIT price, not the lot price",
      row is not None and row["best_bid"] == 0.015625)
check("match_name written at store time", row is not None and row["match_name"] == "sprucelog")

# A null bid must stay null. Storing 0 would say "somebody offers nothing for
# this", which is a different and false claim.
with db.db() as c:
    stone = c.execute("SELECT best_bid FROM ref_market WHERE item_key = 'minecraft:smooth_stone'").fetchone()
check("a missing bid stays NULL and does not become 0", stone["best_bid"] is None)

# --- the mirror is a mirror ------------------------------------------------
# A second cycle that no longer lists an item must drop it. An upsert would
# leave a delisted item sitting there at last week's price with nothing to say
# it was stale.
refmarket.store([PAYLOAD[0]])
with db.db() as c:
    left = c.execute("SELECT COUNT(*) n FROM ref_market").fetchone()["n"]
check("a delisted item disappears on the next cycle", left == 1)

# --- health tells the truth ------------------------------------------------
# The table has a row in it right now. That must NOT be reported as a healthy
# feed, because no cycle has been recorded yet.
check("rows in the table are not mistaken for a working feed",
      "NOT PULLED YET" in refmarket.health())

refmarket.record(rows=1, error=None)
check("a successful cycle reports a success", "last success" in refmarket.health())

refmarket.record(rows=0, error="HTTP 503")
health = refmarket.health()
check("a later failure keeps the last success", "last success" in health)
check("a later failure is still reported", "503" in health)

# --- our side is never touched ---------------------------------------------
honey = catalog.add_item("Spruce Log", 2, price_unit_pieces=1, stack_size=64)
refmarket.store(PAYLOAD)
with db.db() as c:
    ours = c.execute("SELECT price_coins, price_unit_pieces FROM items WHERE id = ?",
                     (honey,)).fetchone()
check("a pull never moves one of our prices",
      ours["price_coins"] == 2 and ours["price_unit_pieces"] == 1)

rows = refmarket.compare()
mine = [r for r in rows if r["name"] == "Spruce Log"]
check("our item is matched to theirs", len(mine) == 1 and mine[0]["ref_demand"] == 107)
check("our own price survives the join", mine and mine[0]["price_coins"] == 2)

# An item of ours they do not list must still appear, with empty columns --
# dropping it would turn "no price for this" into "no such item".
catalog.add_item("Wither Rose Farm Output", 5)
rows = refmarket.compare()
orphan = [r for r in rows if r["name"] == "Wither Rose Farm Output"]
check("an item they do not list still appears",
      len(orphan) == 1 and orphan[0]["ref_price"] is None)

# --- the feed can be switched off ------------------------------------------
check("the source is named, not hardcoded into a message",
      refmarket.SOURCE in refmarket.health())

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print(f"test_refmarket: ok")
