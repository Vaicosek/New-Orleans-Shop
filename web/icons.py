"""Item icons for the storefront grid -- Minecraft's own textures, inlined.

Every icon is a real Minecraft item/block texture, bundled at
`web/assets/icons/*.png` (sourced from PrismarineJS/minecraft-assets, a
community mirror of Minecraft's own art -- these represent items the shop's
players already own on the server, the same way any plugin or wiki shows
them). No runtime fetch: the site must render with the network off
(same rule theme.py's background pattern follows), so icons are read from
disk once, base64-encoded, and cached in memory -- one small file read at
first use, then a pure dict lookup for every request after.

`ITEM_ICON_SLUGS` maps an exact item name (as typed into `/admin` -> Add
item) to the bundled file's stem. An item with no entry -- a new item the
owner adds that this map hasn't caught up to yet -- gets no image request
either: `icon_data_uri()` returns None and the caller falls back to a plain
monogram tile (see storefront.py), never a broken `<img>` or a guessed slug.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets" / "icons"

# Every enchantment on the sheet is sold as an enchanted book -- Minecraft
# has one texture for that, not one per enchantment, so they all share it.
_ENCHANTED_BOOK = "enchanted_book"

ITEM_ICON_SLUGS: dict[str, str] = {
    "Aqua Affinity": _ENCHANTED_BOOK,
    "Blast Protection IV": _ENCHANTED_BOOK,
    "Depth Strider III": _ENCHANTED_BOOK,
    "Feather Falling IV": _ENCHANTED_BOOK,
    "Fire Aspect II": _ENCHANTED_BOOK,
    "Fire Protection IV": _ENCHANTED_BOOK,
    "Fortune III": _ENCHANTED_BOOK,
    "Frost Walker II": _ENCHANTED_BOOK,
    "Knockback I": _ENCHANTED_BOOK,
    "Knockback II": _ENCHANTED_BOOK,
    "Looting III": _ENCHANTED_BOOK,
    "Luck of the Sea": _ENCHANTED_BOOK,
    "Lure III": _ENCHANTED_BOOK,
    "Projectile Protection IV": _ENCHANTED_BOOK,
    "Protection IV": _ENCHANTED_BOOK,
    "Respiration III": _ENCHANTED_BOOK,
    "Sharpness V": _ENCHANTED_BOOK,
    "Silk Touch": _ENCHANTED_BOOK,
    "Smite V": _ENCHANTED_BOOK,
    "Thorns II": _ENCHANTED_BOOK,
    "Mending": _ENCHANTED_BOOK,
    "Swift Sneak": _ENCHANTED_BOOK,
    "Acacia Log": "acacia_log",
    "Birch Log": "birch_log",
    "Cherry Log": "cherry_log",
    "Crimson Stem": "crimson_stem",
    "Dark Oak Log": "dark_oak_log",
    "Jungle Log": "jungle_log",
    "Mangrove Log": "mangrove_log",
    "Oak Log": "oak_log",
    "Pale Oak Log": "pale_oak_log",
    "Spruce Log": "spruce_log",
    "Warped Stem": "warped_stem",
    "Bamboo": "bamboo",
    "Acacia Leaves": "acacia_leaves",
    "Birch Leaves": "birch_leaves",
    "Cherry Leaves": "cherry_leaves",
    "Dark Oak Leaves": "dark_oak_leaves",
    "Jungle Leaves": "jungle_leaves",
    "Mangrove Leaves": "mangrove_leaves",
    "Oak Leaves": "oak_leaves",
    "Pale Oak Leaves": "pale_oak_leaves",
    "Spruce Leaves": "spruce_leaves",
    "Acacia Sapling": "acacia_sapling",
    "Birch Sapling": "birch_sapling",
    "Cherry Sapling": "cherry_sapling",
    "Crimson Fungus": "crimson_fungus",
    "Dark Oak Sapling": "dark_oak_sapling",
    "Jungle Sapling": "jungle_sapling",
    "Mangrove Propagule": "mangrove_propagule",
    "Oak Sapling": "oak_sapling",
    "Pale Oak Sapling": "pale_oak_sapling",
    "Spruce Sapling": "spruce_sapling",
    "Warped Fungus": "warped_fungus",
}


@lru_cache(maxsize=64)
def _data_uri_for_slug(slug: str) -> str | None:
    path = ASSETS_DIR / f"{slug}.png"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def icon_data_uri(item_name: str) -> str | None:
    """The item's icon as an inline `data:` URI, or None if this item has
    no mapped icon yet -- the caller renders a monogram tile instead."""
    slug = ITEM_ICON_SLUGS.get(item_name)
    if slug is None:
        return None
    return _data_uri_for_slug(slug)
