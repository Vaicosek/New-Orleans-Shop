"""Items, prices, stock.

The one rule that matters here: `items.price_coins` is the number the
owner typed and it is stored VERBATIM. Nothing in this module ever divides it
-- `core.pricing.charge()` is the only function in the whole codebase allowed
to turn a price into a per-piece or per-order figure, and every string
this module hands back to a caller runs through `pricing.price_label()` so a
bare price number never reaches a user. AbexTech stores a float per piece and
converts on input; that is the bug this schema makes impossible to write
(see `schema.sql`'s CHECK on `items`), and this module is not allowed to
reintroduce it in code even though the schema can't stop that.

`price_unit_pieces` is how many pieces the typed price buys (64 for
"1g/stack", 32 for "1g/32", 1 for "3g/each") and is the ONLY divisor for a
per-piece figure. `stack_size` is the MINECRAFT stack size and is used only
for capacity/barrel maths -- never as a price divisor. Those two numbers used
to be one column and conflating them is a silent 2x on anything (like
saplings) that stacks to 64 but sells per 32.

`stock.capacity` is a derived number too -- `barrel_slots * stack_size` -- and
this module is the only writer of it, kept in sync every time either factor
changes so nothing downstream (alerts, panels) ever has to recompute it or
risk reading a stale value.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .db import db_in
from .pricing import CURRENCY, charge, price_label


class CatalogError(RuntimeError):
    """Base class. Every failure here is a refusal, not a partial write."""


class NoSuchItem(CatalogError):
    pass


class DuplicateName(CatalogError):
    pass


class OverCapacity(CatalogError):
    """Raised instead of silently clamping stock to the barrel's capacity."""


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonneg_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


# ------------------------------------------------------------------ items

def add_item(name: str, price_coins: int, *, price_unit_pieces: int = 64,
             stack_size: int = 64, stackable: bool = True, barrel_slots: int = 54,
             category: str | None = None, subcategory: str | None = None,
             sort_order: int = 0, slots: int | None = None,
             supply_source: str | None = None,
             conn: Optional[sqlite3.Connection] = None) -> int:
    """Add an item. `price_coins` is written exactly as given -- never
    divided, never converted to a per-piece float. `price_unit_pieces` is how
    many pieces that price buys and is the only number `pricing.charge()` may
    divide by; `stack_size` is the Minecraft stack size and feeds capacity
    only. Capacity for the new item's stock row is derived once here from
    `barrel_slots * stack_size`, the same formula `_sync_capacity` uses on
    every later update, so there is only ever one place that formula lives.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name must not be empty")
    price_coins = _nonneg_int(price_coins, "price_coins")
    price_unit_pieces = _positive_int(price_unit_pieces, "price_unit_pieces")
    stack_size = _positive_int(stack_size, "stack_size")
    barrel_slots = _positive_int(barrel_slots, "barrel_slots")
    if not stackable and stack_size != 1:
        raise ValueError("a non-stackable item must have stack_size = 1")
    if price_unit_pieces > stack_size:
        raise ValueError("price_unit_pieces must not exceed stack_size")
    if slots is not None:
        slots = _positive_int(slots, "slots")
    sort_order = _nonneg_int(sort_order, "sort_order")

    with db_in(conn) as c:
        try:
            cur = c.execute(
                "INSERT INTO items (name, price_coins, price_unit_pieces, stack_size, "
                "stackable, barrel_slots, category, subcategory, sort_order, slots, "
                "supply_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, price_coins, price_unit_pieces, stack_size, 1 if stackable else 0,
                 barrel_slots, category, subcategory, sort_order, slots, supply_source),
            )
        except sqlite3.IntegrityError as err:
            raise DuplicateName(f"an item named {name!r} already exists") from err
        item_id = cur.lastrowid
        capacity = barrel_slots * stack_size
        c.execute(
            "INSERT INTO stock (item_id, pieces, capacity) VALUES (?, 0, ?)",
            (item_id, capacity),
        )
        return item_id


def update_item(item_id: int, *, name: str | None = None,
                 price_coins: int | None = None,
                 price_unit_pieces: int | None = None,
                 stack_size: int | None = None,
                 stackable: bool | None = None,
                 barrel_slots: int | None = None,
                 category: str | None = None,
                 subcategory: str | None = None,
                 sort_order: int | None = None,
                 slots: int | None = None,
                 supply_source: str | None = None,
                 conn: Optional[sqlite3.Connection] = None) -> None:
    """Update an item's fields. `price_coins`, if given, replaces the
    stored number verbatim -- repricing never touches anything already
    snapshotted onto an in-flight order (see `orders.create_order`).

    If `stack_size` or `barrel_slots` changes, capacity is recomputed and
    resynced onto `stock` in the SAME transaction. If the new capacity would
    be smaller than the pieces currently on hand, this raises `OverCapacity`
    rather than silently truncating stock to fit -- a shrink that clamped
    quietly would just be `MAX(0, x)` in disguise, and the schema's whole
    point is to force truncation to be a decision, not an accident.
    """
    with db_in(conn) as c:
        item = c.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise NoSuchItem(f"no such item {item_id}")

        fields: dict[str, Any] = {}
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("name must not be empty")
            fields["name"] = name
        if price_coins is not None:
            fields["price_coins"] = _nonneg_int(price_coins, "price_coins")
        if price_unit_pieces is not None:
            fields["price_unit_pieces"] = _positive_int(price_unit_pieces, "price_unit_pieces")
        if stack_size is not None:
            fields["stack_size"] = _positive_int(stack_size, "stack_size")
        if stackable is not None:
            fields["stackable"] = 1 if stackable else 0
        if barrel_slots is not None:
            fields["barrel_slots"] = _positive_int(barrel_slots, "barrel_slots")
        if category is not None:
            fields["category"] = category
        if subcategory is not None:
            fields["subcategory"] = subcategory
        if sort_order is not None:
            fields["sort_order"] = _nonneg_int(sort_order, "sort_order")
        if slots is not None:
            fields["slots"] = _positive_int(slots, "slots")
        if supply_source is not None:
            fields["supply_source"] = supply_source

        new_stack_size = fields.get("stack_size", item["stack_size"])
        new_stackable = fields.get("stackable", item["stackable"])
        if not new_stackable and new_stack_size != 1:
            raise ValueError("a non-stackable item must have stack_size = 1")
        new_unit_pieces = fields.get("price_unit_pieces", item["price_unit_pieces"])
        if new_unit_pieces > new_stack_size:
            raise ValueError("price_unit_pieces must not exceed stack_size")

        if not fields:
            return

        try:
            set_clause = ", ".join(f"{k} = :{k}" for k in fields)
            c.execute(
                f"UPDATE items SET {set_clause}, updated_at = datetime('now') WHERE id = :id",
                {**fields, "id": item_id},
            )
        except sqlite3.IntegrityError as err:
            raise DuplicateName(f"an item named {fields.get('name')!r} already exists") from err

        if "stack_size" in fields or "barrel_slots" in fields:
            new_barrel_slots = fields.get("barrel_slots", item["barrel_slots"])
            _sync_capacity(c, item_id, new_barrel_slots, new_stack_size)


def _sync_capacity(conn: sqlite3.Connection, item_id: int,
                    barrel_slots: int, stack_size: int) -> None:
    capacity = barrel_slots * stack_size
    cur = conn.execute(
        "UPDATE stock SET capacity = :cap, updated_at = datetime('now') "
        " WHERE item_id = :id AND pieces <= :cap",
        {"cap": capacity, "id": item_id},
    )
    if cur.rowcount != 1:
        row = conn.execute("SELECT pieces FROM stock WHERE item_id = ?", (item_id,)).fetchone()
        on_hand = row["pieces"] if row else 0
        raise OverCapacity(
            f"item {item_id} has {on_hand} pieces on hand; new capacity {capacity} is smaller"
        )


def deactivate_item(item_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Deactivate an item. Deactivated items drop out of `search()` and the
    picker; existing orders and their price snapshots are untouched."""
    with db_in(conn) as c:
        cur = c.execute("UPDATE items SET active = 0, updated_at = datetime('now') WHERE id = ?",
                         (item_id,))
        if cur.rowcount != 1:
            raise NoSuchItem(f"no such item {item_id}")


def get_item(item_id: int, *, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NoSuchItem(f"no such item {item_id}")
    return _row(row)


def get_item_by_name(name: str, *, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any] | None:
    """Exact-name lookup (case-sensitive, matching the UNIQUE constraint) --
    used by idempotent seeders that need "does this item already exist"
    without pulling in `search()`'s fuzzy matching.
    """
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM items WHERE name = ?", (name,)).fetchone()
    return _row(row) if row is not None else None


_ITEM_ORDER = "category, sort_order, subcategory, name"


def list_items(*, active_only: bool = True,
               conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Every item, in shop-sheet order: category, then sort_order, then
    subcategory, then name -- so a category with sub-groups (logs, leaves,
    saplings) never interleaves alphabetically across them.
    """
    with db_in(conn) as c:
        if active_only:
            rows = c.execute(
                f"SELECT * FROM items WHERE active = 1 ORDER BY {_ITEM_ORDER}"
            ).fetchall()
        else:
            rows = c.execute(f"SELECT * FROM items ORDER BY {_ITEM_ORDER}").fetchall()
    return [_row(r) for r in rows]


# ------------------------------------------------------------------ stock

def get_stock(item_id: int, *, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM stock WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise NoSuchItem(f"no such item {item_id}")
    return _row(row)


def set_stock(item_id: int, pieces: int, *,
              conn: Optional[sqlite3.Connection] = None) -> None:
    """Set the absolute quantity on hand.

    Bounded to capacity in the same UPDATE ... WHERE rather than clamped --
    an attempt to set more pieces than the barrel can hold is a caller bug
    (a miscounted delivery, a wrong item picked) and must fail loud, not
    silently cap at capacity and hide the discrepancy.
    """
    pieces = _nonneg_int(pieces, "pieces")
    with db_in(conn) as c:
        cur = c.execute(
            "UPDATE stock SET pieces = :p, updated_at = datetime('now') "
            " WHERE item_id = :id AND :p <= capacity",
            {"p": pieces, "id": item_id},
        )
        if cur.rowcount == 1:
            return
        row = c.execute("SELECT capacity FROM stock WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            raise NoSuchItem(f"no such item {item_id}")
        raise OverCapacity(f"item {item_id} capacity is {row['capacity']}, cannot set {pieces}")


def adjust_stock(item_id: int, delta: int, *,
                  conn: Optional[sqlite3.Connection] = None) -> int:
    """Adjust stock by `delta` (positive or negative) in one atomic UPDATE,
    so two concurrent restock/withdrawal events cannot race a read-then-write
    and lose one side's change. Returns the resulting quantity.
    """
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise TypeError(f"delta must be an int, got {type(delta).__name__}")
    if delta == 0:
        raise ValueError("delta must not be zero")
    with db_in(conn) as c:
        cur = c.execute(
            "UPDATE stock SET pieces = pieces + :d, updated_at = datetime('now') "
            " WHERE item_id = :id AND pieces + :d >= 0 AND pieces + :d <= capacity",
            {"d": delta, "id": item_id},
        )
        if cur.rowcount != 1:
            row = c.execute("SELECT pieces, capacity FROM stock WHERE item_id = ?",
                             (item_id,)).fetchone()
            if row is None:
                raise NoSuchItem(f"no such item {item_id}")
            raise OverCapacity(
                f"item {item_id} has {row['pieces']}/{row['capacity']}, "
                f"cannot adjust by {delta}"
            )
        return c.execute("SELECT pieces FROM stock WHERE item_id = ?", (item_id,)).fetchone()["pieces"]


def capacity_of(item_id: int, *, conn: Optional[sqlite3.Connection] = None) -> int:
    return get_stock(item_id, conn=conn)["capacity"]


# ------------------------------------------------------------------ search & quotes

def search(term: str, *, active_only: bool = True, limit: int = 20,
           conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Fuzzy/substring search over item names for a Discord picker.

    A user must never need the exact name: this matches any substring,
    case-insensitively, ranked so a name that STARTS WITH the term (or
    matches earliest) sorts first. Every row includes `label`, a ready-to-show
    picker string that already runs the price through `pricing.price_label()`
    -- nothing upstream should ever need to touch `price_coins` directly
    to build a picker option.
    """
    term = (term or "").strip()
    like = f"%{term}%"
    active_clause = "AND i.active = 1" if active_only else ""
    with db_in(conn) as c:
        rows = c.execute(
            f"SELECT i.*, s.pieces AS qty, s.capacity AS capacity "
            f"  FROM items i LEFT JOIN stock s ON s.item_id = i.id "
            f" WHERE i.name LIKE :like {active_clause} "
            f" ORDER BY instr(lower(i.name), lower(:term)), length(i.name), i.name "
            f" LIMIT :limit",
            {"like": like, "term": term, "limit": int(limit)},
        ).fetchall()
    results = []
    for r in rows:
        d = _row(r)
        d["label"] = (f"{d['name']} — "
                      f"{price_label(d['price_coins'], d['price_unit_pieces'], d['stack_size'])}")
        results.append(d)
    return results


def quote(item_id: int, pieces: int, *,
          conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Price `pieces` pieces of `item_id` at its CURRENT catalog price.

    All arithmetic is delegated to `pricing.charge()` -- this function only
    looks up the item and formats the result. `price_label` supplies the
    display text so nothing here ever hands a caller a bare number.
    """
    pieces = _nonneg_int(pieces, "pieces")
    item = get_item(item_id, conn=conn)
    total = charge(pieces, item["price_coins"], item["price_unit_pieces"])
    return {
        "item_id": item_id,
        "name": item["name"],
        "pieces": pieces,
        "total_coins": total,
        "price_label": price_label(item["price_coins"], item["price_unit_pieces"], item["stack_size"]),
        "total_label": f"{total:,} {CURRENCY} for {pieces} × {item['name']}",
    }


# ------------------------------------------------------------------ categories

def upsert_category(name: str, sort_order: int, *, note: str | None = None,
                     conn: Optional[sqlite3.Connection] = None) -> None:
    """Create or update a category row. A category may exist with zero items
    -- that is how a PLANNED section gets recorded before anything is
    stocked in it -- so this never touches `items`.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name must not be empty")
    sort_order = _nonneg_int(sort_order, "sort_order")
    with db_in(conn) as c:
        c.execute(
            "INSERT INTO categories (name, sort_order, note) VALUES (:name, :sort, :note) "
            "ON CONFLICT(name) DO UPDATE SET sort_order = excluded.sort_order, "
            "note = excluded.note",
            {"name": name, "sort": sort_order, "note": note},
        )


def list_categories(*, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Every declared category, in shop-sheet order. Includes empty ones --
    callers decide for themselves whether their audience should see those
    (see `categories_with_items`).
    """
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT name, sort_order, note FROM categories ORDER BY sort_order, name"
        ).fetchall()
    return [_row(r) for r in rows]


def subcategory_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split an item list (already in `category, sort_order, subcategory,
    name` order) into consecutive runs sharing the same `subcategory`,
    preserving order. Each run is `{"subcategory": <name or None>, "slots":
    <sum of that run's slots, or None>, "items": [...]}`.

    `slots` is the owner's own bookkeeping -- the sub-group's slot count off
    his price sheet ("logs - 12 slots") -- summed here once so every caller
    (storefront, ledger, admin) reads the same number instead of re-summing
    it independently. It is `None` when every item in the run has a NULL
    `slots`, so a caller can tell "no data" apart from "genuinely zero".
    """
    groups: list[dict[str, Any]] = []
    for item in items:
        key = item.get("subcategory")
        if not groups or groups[-1]["subcategory"] != key:
            groups.append({"subcategory": key, "items": []})
        groups[-1]["items"].append(item)
    for g in groups:
        vals = [i["slots"] for i in g["items"] if i.get("slots") is not None]
        g["slots"] = sum(vals) if vals else None
    return groups


def categories_with_items(*, active_only: bool = True, include_empty: bool = True,
                           conn: Optional[sqlite3.Connection] = None
                           ) -> list[dict[str, Any]]:
    """Categories in shop-sheet order, each carrying its own `items` list
    (itself in `sort_order, subcategory, name` order -- see `_ITEM_ORDER`)
    and a `groups` list already split by `subcategory_groups()`, so a
    caller that wants to render category -> subcategory -> items never has
    to re-sort or re-group.

    `include_empty=True` (the default) is the staff view -- a planned
    category with no items yet is a to-do list entry, and its absence would
    hide that fact. Pass `include_empty=False` for the public storefront:
    a category nobody has stocked is not shown to a customer, not even as a
    bare heading, because an empty heading tells them nothing.

    An item whose `category` is not (or not yet) a row in `categories` still
    appears, grouped under its own name, sorted after every declared
    category -- so a stray item is visible somewhere rather than silently
    dropped.
    """
    with db_in(conn) as c:
        cats = list_categories(conn=c)
        item_rows = c.execute(
            f"SELECT * FROM items {'WHERE active = 1' if active_only else ''} "
            f"ORDER BY {_ITEM_ORDER}"
        ).fetchall()

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in item_rows:
        d = _row(r)
        by_cat.setdefault(d["category"] or "Uncategorized", []).append(d)

    known = {cat["name"] for cat in cats}
    result: list[dict[str, Any]] = []
    for cat in cats:
        items = by_cat.get(cat["name"], [])
        if not items and not include_empty:
            continue
        result.append({**cat, "items": items, "groups": subcategory_groups(items)})

    for name in sorted(k for k in by_cat if k not in known):
        items = by_cat[name]
        result.append({"name": name, "sort_order": None, "note": None,
                        "items": items, "groups": subcategory_groups(items)})

    return result
