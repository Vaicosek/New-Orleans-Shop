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
    ("Random items", 9, None),
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


# ---------------------------------------------------------------------------
# Catalog expansion (Potions, Coloured blocks, Mob drops, Stone adjacent,
# Tools armour and weapons, Natural blocks, Random items) -- from the
# owner's price-sheet message. Two structural decisions were confirmed with
# him rather than guessed:
#   - Potions: every vanilla effect gets TWO listings, a Regular (1g per 9
#     pieces) and a Splash (1g per potion) -- including "Ghost Walker" as an
#      8th real splash-potion listing at 2g, priced to match a rival shop.
#   - Diamond tools (Axe/Pickaxe/Shovel/Hoe): ONE listing per tool at 15g;
#     the buyer picks silk touch or fortune when ordering (noted in
#     supply_source), not two separate catalog rows.
#   - Netherite armour: all 4 pieces (Helmet/Chestplate/Leggings/Boots),
#     each its own 39g listing.
# ---------------------------------------------------------------------------
ITEMS.extend([
    ('Saturation', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Fire Resistance', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Invisibility', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Health Boost', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Absorption', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Haste', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Health', dict(price_coins=1, price_unit_pieces=9, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Regular', sort_order=0, supply_source='auto brewers')),
    ('Splash Saturation', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Splash Fire Resistance', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Splash Invisibility', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Splash Health Boost', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Splash Absorption', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Splash Haste', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Splash Health', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='auto brewers')),
    ('Ghost Walker', dict(price_coins=2, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Potions', subcategory='Splash', sort_order=1, supply_source='priced to match a rival shop (Quebec)')),
    ('White Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Orange Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Magenta Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Light Blue Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Yellow Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Lime Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Pink Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Gray Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Light Gray Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Cyan Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Purple Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Blue Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Brown Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Green Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Red Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('Black Concrete', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Concrete', sort_order=0, supply_source=None)),
    ('White Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Orange Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Magenta Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Light Blue Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Yellow Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Lime Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Pink Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Gray Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Light Gray Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Cyan Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Purple Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Blue Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Brown Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Green Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Red Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('Black Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Terracotta', sort_order=1, supply_source=None)),
    ('White Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Orange Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Magenta Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Light Blue Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Yellow Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Lime Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Pink Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Gray Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Light Gray Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Cyan Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Purple Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Blue Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Brown Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Green Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Red Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('Black Glazed Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Glazed Terracotta', sort_order=2, supply_source=None)),
    ('White Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Orange Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Magenta Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Light Blue Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Yellow Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Lime Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Pink Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Gray Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Light Gray Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Cyan Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Purple Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Blue Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Brown Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Green Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Red Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('Black Wool', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Wool', sort_order=3, supply_source=None)),
    ('White Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Orange Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Magenta Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Light Blue Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Yellow Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Lime Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Pink Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Gray Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Light Gray Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Cyan Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Purple Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Blue Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Brown Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Green Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Red Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('Black Stained Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Stained Glass', sort_order=4, supply_source=None)),
    ('White Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Orange Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Magenta Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Light Blue Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Yellow Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Lime Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Pink Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Gray Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Light Gray Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Cyan Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Purple Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Blue Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Brown Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Green Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Red Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Black Dye', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Dye', sort_order=5, supply_source=None)),
    ('Glass', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Other', sort_order=6, supply_source=None)),
    ('Terracotta', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Coloured blocks', subcategory='Other', sort_order=6, supply_source=None)),
    ('Slimeball', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Mob drops', subcategory=None, sort_order=0, supply_source=None)),
    ('Gunpowder', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Mob drops', subcategory=None, sort_order=0, supply_source=None)),
    ('Spider Eye', dict(price_coins=1, price_unit_pieces=32, stack_size=64, stackable=True, slots=1, category='Mob drops', subcategory=None, sort_order=0, supply_source=None)),
    ('Bone', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Mob drops', subcategory=None, sort_order=0, supply_source=None)),
    ('Leather', dict(price_coins=1, price_unit_pieces=32, stack_size=64, stackable=True, slots=1, category='Mob drops', subcategory=None, sort_order=0, supply_source=None)),
    ('Rotten Flesh', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Mob drops', subcategory=None, sort_order=0, supply_source=None)),
    ('Stone', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Smooth Stone', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Andesite', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Diorite', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Granite', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Tuff', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Dripstone Block', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Cobbled Deepslate', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Sand', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Sandstone', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Pointed Dripstone', dict(price_coins=1, price_unit_pieces=32, stack_size=64, stackable=True, slots=1, category='Stone adjacent', subcategory=None, sort_order=0, supply_source=None)),
    ('Diamond Axe', dict(price_coins=15, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Tools', sort_order=0, supply_source="silk touch or fortune (buyer's choice), efficiency 5, unbreaking 3, mending")),
    ('Diamond Pickaxe', dict(price_coins=15, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Tools', sort_order=0, supply_source="silk touch or fortune (buyer's choice), efficiency 5, unbreaking 3, mending")),
    ('Diamond Shovel', dict(price_coins=15, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Tools', sort_order=0, supply_source="silk touch or fortune (buyer's choice), efficiency 5, unbreaking 3, mending")),
    ('Diamond Hoe', dict(price_coins=15, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Tools', sort_order=0, supply_source="silk touch or fortune (buyer's choice), efficiency 5, unbreaking 3, mending")),
    ('Diamond Helmet', dict(price_coins=5, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Armour', sort_order=1, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Diamond Chestplate', dict(price_coins=5, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Armour', sort_order=1, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Diamond Leggings', dict(price_coins=5, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Armour', sort_order=1, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Diamond Boots', dict(price_coins=5, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Diamond Armour', sort_order=1, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Netherite Helmet', dict(price_coins=39, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Netherite Armour', sort_order=2, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Netherite Chestplate', dict(price_coins=39, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Netherite Armour', sort_order=2, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Netherite Leggings', dict(price_coins=39, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Netherite Armour', sort_order=2, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Netherite Boots', dict(price_coins=39, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Netherite Armour', sort_order=2, supply_source='maxed protection enchants, unbreaking 3, mending')),
    ('Diamond Sword', dict(price_coins=12, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Weapons', sort_order=3, supply_source='sharpness 5, unbreaking 3, mending')),
    ('Diamond Sword (Fire Aspect)', dict(price_coins=15, price_unit_pieces=1, stack_size=1, stackable=False, slots=1, category='Tools armour and weapons', subcategory='Weapons', sort_order=3, supply_source='sharpness 5, unbreaking 3, mending, fire aspect 2')),
    ('Arrow', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Tools armour and weapons', subcategory='Weapons', sort_order=3, supply_source=None)),
    ('TNT', dict(price_coins=1, price_unit_pieces=16, stack_size=64, stackable=True, slots=1, category='Tools armour and weapons', subcategory='Weapons', sort_order=3, supply_source=None)),
    ('Moss Block', dict(price_coins=1, price_unit_pieces=32, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Moss', sort_order=0, supply_source=None)),
    ('Pale Moss Block', dict(price_coins=1, price_unit_pieces=32, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Moss', sort_order=0, supply_source=None)),
    ('Dandelion', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Poppy', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Blue Orchid', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Allium', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Azure Bluet', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Red Tulip', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Orange Tulip', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('White Tulip', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Pink Tulip', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Oxeye Daisy', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Cornflower', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Lily of the Valley', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Wither Rose', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Sunflower', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Lilac', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Rose Bush', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Peony', dict(price_coins=1, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Flowers', sort_order=1, supply_source=None)),
    ('Cactus', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Other', sort_order=2, supply_source=None)),
    ('Nether Wart', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Natural blocks', subcategory='Other', sort_order=2, supply_source=None)),
    ('Honey Block', dict(price_coins=1, price_unit_pieces=16, stack_size=64, stackable=True, slots=1, category='Random items', subcategory=None, sort_order=0, supply_source=None)),
    ('Honeycomb', dict(price_coins=1, price_unit_pieces=32, stack_size=64, stackable=True, slots=1, category='Random items', subcategory=None, sort_order=0, supply_source=None)),
    ('Beehive', dict(price_coins=16, price_unit_pieces=1, stack_size=64, stackable=True, slots=1, category='Random items', subcategory=None, sort_order=0, supply_source=None)),
    ('Ochre Froglight', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Random items', subcategory=None, sort_order=0, supply_source=None)),
    ('Verdant Froglight', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Random items', subcategory=None, sort_order=0, supply_source=None)),
    ('Pearlescent Froglight', dict(price_coins=1, price_unit_pieces=64, stack_size=64, stackable=True, slots=1, category='Random items', subcategory=None, sort_order=0, supply_source=None)),
])

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
