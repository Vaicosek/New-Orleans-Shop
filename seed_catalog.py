#!/usr/bin/env python3
"""Idempotent catalog seed for New Orleans.

Run: `python3 seed_catalog.py` (respects NOLA_DB_PATH like everything else,
so a scratch DB is just `NOLA_DB_PATH=/tmp/whatever.db python3 seed_catalog.py`).

Safe to re-run: every item is upserted BY NAME (`catalog.get_item_by_name`,
then `add_item` if new or `update_item` if it already exists), and every
category is upserted the same way via `catalog.upsert_category`. Running
this twice in a row must leave the catalog identical to running it once.

Loads EXACTLY the data the owner gave -- nothing here is invented, added,
leveled up, or "corrected". Every place the source data was ambiguous is
flagged in a comment next to the entry it applies to, not silently resolved
in one direction.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import catalog, db                                      # noqa: E402
from core.pricing import price_label                              # noqa: E402

db.init_db()

# ---------------------------------------------------------------- categories
# In shop-sheet order. Only "Enchanted books" and "Wood" carry items today;
# the rest are declared PLANNED so they show up as a to-do list on staff
# pages (`/ledger`, the bot admin panel) even though the public storefront
# hides an empty category entirely.
CATEGORIES: list[tuple[str, int, str | None]] = [
    ("Enchanted books", 0, None),
    ("Wood", 1, None),
    ("Potions", 2, None),
    ("Coloured blocks", 3, None),
    ("Mob drops", 4, None),
    ("Stone adjacent", 5, None),
    ("Ores", 6, None),
    ("Tools armour and weapons", 7, None),
    ("Natural blocks", 8, None),
]

# ---------------------------------------------------------------- items
# (name, {add_item/update_item kwargs, minus name})
ITEMS: list[tuple[str, dict]] = []


def _books(names: list[str], *, price_coins: int, supply_source: str,
           subcategory: str, sort_order: int) -> None:
    for n in names:
        ITEMS.append((n, dict(
            price_coins=price_coins, price_unit_pieces=1, stack_size=1,
            stackable=False, slots=1, category="Enchanted books",
            subcategory=subcategory, sort_order=sort_order,
            supply_source=supply_source,
        )))


_books([
    "Aqua Affinity", "Blast Protection IV", "Depth Strider III",
    "Feather Falling IV", "Fire Protection IV", "Frost Walker II",
    "Projectile Protection IV", "Protection IV", "Respiration III",
    "Thorns II", "Fire Aspect II", "Looting III", "Sharpness V",
    "Smite V", "Knockback II", "Knockback I", "Fortune III",
    "Silk Touch", "Lure III",
    "Luck of the Sea",
    # NOTE: he wrote "luck of sea" with no level -- entered without one.
    # Do NOT guess III; ask him if a level was intended.
], price_coins=3, supply_source="needs extensive villager farm",
   subcategory="Regular", sort_order=0)

# "more expensive books" -- same category, same per-item shape (slots=1),
# priced and supplied differently from the regular run.
_books(
    ["Mending"], price_coins=3, supply_source="rare — not villager-farmable",
    subcategory="Rare", sort_order=1,
)
# NOTE: he grouped Mending under "more expensive books" but wrote 3g -- the
# same price as a regular book. Entered exactly as written; flag this for
# him to confirm whether 3g was a typo for Mending.
_books(
    ["Swift Sneak"], price_coins=50, supply_source="rare — not villager-farmable",
    subcategory="Rare", sort_order=1,
)


def _wood(names: list[str], *, price_coins: int, unit_pieces: int, stack_size: int,
          subcategory: str, sort_order: int) -> None:
    for n in names:
        ITEMS.append((n, dict(
            price_coins=price_coins, price_unit_pieces=unit_pieces,
            stack_size=stack_size, stackable=True, slots=1,
            category="Wood", subcategory=subcategory, sort_order=sort_order,
            supply_source="wither tree farm",
        )))


_wood([
    "Bamboo", "Warped Stem", "Crimson Stem", "Cherry Log", "Mangrove Log",
    "Jungle Log", "Acacia Log", "Pale Oak Log", "Birch Log", "Oak Log",
    "Dark Oak Log", "Spruce Log",
], price_coins=1, unit_pieces=64, stack_size=64, subcategory="Logs", sort_order=0)
# He wrote "warped, crimson" -- those are Stems, not Logs, in real Minecraft
# naming. Real names used, per his standing rule.

_wood([
    "Cherry Leaves", "Mangrove Leaves", "Jungle Leaves", "Acacia Leaves",
    "Pale Oak Leaves", "Birch Leaves", "Oak Leaves", "Dark Oak Leaves",
    "Spruce Leaves",
], price_coins=1, unit_pieces=64, stack_size=64, subcategory="Leaves", sort_order=1)

_SAPLINGS = [
    "Bamboo",  # collides with the Bamboo log above -- see NOTE below.
    "Warped Fungus", "Crimson Fungus", "Cherry Sapling", "Mangrove Propagule",
    "Jungle Sapling", "Acacia Sapling", "Pale Oak Sapling", "Birch Sapling",
    "Oak Sapling", "Dark Oak Sapling", "Spruce Sapling",
]
# NOTE: "Bamboo" already exists as a log (his 12-slot log list also lists
# Bamboo). Minecraft has exactly one "Bamboo" item -- there is no separate
# sapling form -- so a second row here would collide on items.name's UNIQUE
# constraint. Resolved by NOT adding a second Bamboo row for saplings; the
# Bamboo row seeded above (price_unit_pieces=64, under Wood/logs) stands for
# it. His "12 slots" of saplings is therefore 11 distinct catalog rows plus
# the one already-seeded Bamboo. Warped/Crimson "saplings" are Fungi in real
# Minecraft naming; real names used.
_wood(
    [n for n in _SAPLINGS if n != "Bamboo"],
    price_coins=1, unit_pieces=32, stack_size=64,
    subcategory="Saplings", sort_order=2,
)

assert len(ITEMS) == len({n for n, _ in ITEMS}), "duplicate item name in seed data"


def upsert_item(name: str, fields: dict) -> tuple[int, bool]:
    """Insert or update by exact name. Returns (item_id, was_created)."""
    existing = catalog.get_item_by_name(name)
    if existing is None:
        item_id = catalog.add_item(name, fields["price_coins"],
                                    **{k: v for k, v in fields.items() if k != "price_coins"})
        return item_id, True
    catalog.update_item(existing["id"], **fields)
    return existing["id"], False


def main() -> None:
    for name, sort_order, note in CATEGORIES:
        catalog.upsert_category(name, sort_order, note=note)

    created = updated = 0
    per_category: dict[str, int] = {}
    for name, fields in ITEMS:
        _id, was_created = upsert_item(name, fields)
        created += was_created
        updated += not was_created
        per_category[fields["category"]] = per_category.get(fields["category"], 0) + 1

    print(f"categories: {len(CATEGORIES)} declared")
    print(f"items: {len(ITEMS)} in seed data -- {created} created, {updated} updated")
    for cat_name, count in per_category.items():
        print(f"  {cat_name}: {count} item(s) seeded")

    print()
    print("Catalog by category (price_label() applied to every row):")
    for cat in catalog.categories_with_items(active_only=False, include_empty=True):
        print(f"\n[{cat['name']}]")
        if not cat["items"]:
            note = f" ({cat['note']})" if cat.get("note") else ""
            print(f"  planned -- no items yet{note}")
            continue
        for item in cat["items"]:
            label = price_label(item["price_coins"], item["price_unit_pieces"], item["stack_size"])
            print(f"  {item['name']}: {label}")


if __name__ == "__main__":
    main()
