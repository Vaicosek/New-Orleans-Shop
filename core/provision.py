"""The Discord layout `/setup` builds, and the record of what it built.

Why this exists at all: `core.config` used to require SHOP_CHANNEL_ID,
ORDERS_CHANNEL_ID and ALERTS_CHANNEL_ID before the bot would start. On a fresh
server those channels do not exist yet, so there was nothing to put in them --
and Wispbyte has no shell to add them from afterwards. The bot now builds its
own layout and remembers the ids.

**No discord.py in this module.** Planning is pure: it takes a snapshot of
what already exists, compares it against DESIRED, and returns a plan. The bot
layer (`bot/views/setup.py`) is the only thing that talks to the Discord API.
That seam is what lets the interesting half be tested without a gateway.

Idempotency is the whole point -- `/setup` is going to be run twice by
someone, and the second run must be boring. Resolution order for each item:

  1. an id already in `guild_layout` that still exists in the guild  -> OK
  2. an existing thing with exactly the matching name                -> ADOPT
  3. neither                                                          -> CREATE

Adoption matters more than it looks. Someone who already made a #shop channel
by hand should not end up with a second one called #shop; Discord allows the
duplicate and the pair is then indistinguishable in the picker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Sequence

from core.db import db_in

Kind = Literal["category", "channel", "role"]
Action = Literal["ok", "adopt", "create"]


@dataclass(frozen=True)
class Desired:
    key: str            # our stable name, e.g. "channel:shop"
    kind: Kind
    name: str           # what it is called in Discord
    parent: str | None = None   # key of the category it belongs in
    topic: str | None = None
    staff_only: bool = False    # hidden from @everyone
    env_name: str | None = None # the env var this used to come from


# The layout, as data. Ordering matters: categories before the channels that
# name them as a parent, because creating a channel needs its category's id.
DESIRED: tuple[Desired, ...] = (
    Desired("role:staff", "role", "Staff"),
    Desired("role:manager", "role", "Manager"),

    Desired("category:market", "category", "New Orleans"),
    Desired("channel:welcome", "channel", "welcome", parent="category:market",
            topic="What New Orleans is, and how to buy."),
    Desired("channel:shop", "channel", "shop", parent="category:market",
            topic="The shop panel. /shop", env_name="SHOP_CHANNEL_ID"),
    Desired("channel:orders", "channel", "orders", parent="category:market",
            topic="Open orders and claims. /orders", env_name="ORDERS_CHANNEL_ID"),

    Desired("category:games", "category", "Games"),
    Desired("channel:casino", "channel", "casino", parent="category:games",
            topic="Coinflip and dice, provably fair. /casino"),
    Desired("channel:predictions", "channel", "predictions", parent="category:games",
            topic="Prediction markets. /predict"),

    Desired("category:staff", "category", "Staff", staff_only=True),
    Desired("channel:alerts", "channel", "alerts", parent="category:staff",
            topic="Restock alerts.", staff_only=True, env_name="ALERTS_CHANNEL_ID"),
    Desired("channel:audit-log", "channel", "audit-log", parent="category:staff",
            topic="What happened, who did it.", staff_only=True),
)

DESIRED_BY_KEY = {d.key: d for d in DESIRED}


@dataclass(frozen=True)
class Step:
    """One row of the preview. `action` is what will happen, and it is shown
    to the owner BEFORE anything is created -- section 8 rule 10: preview with
    real figures, then confirm."""
    desired: Desired
    action: Action
    existing_id: Optional[int] = None
    existing_name: Optional[str] = None

    @property
    def key(self) -> str:
        return self.desired.key


# ---------------------------------------------------------------- storage

def record(guild_id: int, key: str, discord_id: int, name: str, conn=None) -> None:
    """Remember one built thing. Last write wins: re-running setup after
    someone deleted a channel must be able to point the key at the new one."""
    with db_in(conn) as c:
        c.execute(
            "INSERT INTO guild_layout (guild_id, key, discord_id, name) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET "
            "  discord_id = excluded.discord_id, name = excluded.name",
            (int(guild_id), key, int(discord_id), name),
        )


def stored(guild_id: int, conn=None) -> dict[str, int]:
    """Every key this guild has an id for."""
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT key, discord_id FROM guild_layout WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchall()
    return {r["key"]: int(r["discord_id"]) for r in rows}


def channel_id(guild_id: int, key: str, conn=None) -> Optional[int]:
    """The id for one key, or None. This is what the boot self-check and the
    alert scanner read instead of an env var."""
    with db_in(conn) as c:
        row = c.execute(
            "SELECT discord_id FROM guild_layout WHERE guild_id = ? AND key = ?",
            (int(guild_id), key),
        ).fetchone()
    return int(row["discord_id"]) if row else None


def forget(guild_id: int, key: str, conn=None) -> None:
    """Drop a stale mapping -- used when a stored id no longer resolves, so
    the next setup adopts or creates rather than pointing at a dead channel."""
    with db_in(conn) as c:
        c.execute(
            "DELETE FROM guild_layout WHERE guild_id = ? AND key = ?",
            (int(guild_id), key),
        )


# ---------------------------------------------------------------- planning

def plan(
    guild_id: int,
    live_ids: Iterable[int],
    existing_by_name: dict[tuple[str, str], int],
    stored_ids: Optional[dict[str, int]] = None,
) -> list[Step]:
    """Work out what `/setup` would do, without doing any of it.

    `live_ids`          - every channel/role/category id that currently exists
                          in the guild. A stored id absent from this set is
                          stale: the thing was deleted since we made it.
    `existing_by_name`  - {(kind, lowercased name): id} for adoption.
    `stored_ids`        - override for tests; read from the DB otherwise.

    Returns one Step per DESIRED item, in DESIRED order, so a caller can
    create categories before the channels that sit in them.
    """
    have = set(int(i) for i in live_ids)
    known = stored_ids if stored_ids is not None else stored(guild_id)
    steps: list[Step] = []

    for d in DESIRED:
        prior = known.get(d.key)
        if prior is not None and int(prior) in have:
            steps.append(Step(d, "ok", existing_id=int(prior)))
            continue
        # Either never built, or built and since deleted. Adopt a same-named
        # one if the owner already made it by hand; Discord will happily
        # create a second #shop otherwise and nobody can tell them apart.
        adopted = existing_by_name.get((d.kind, d.name.lower()))
        if adopted is not None:
            steps.append(Step(d, "adopt", existing_id=int(adopted), existing_name=d.name))
        else:
            steps.append(Step(d, "create"))
    return steps


def summarise(steps: Sequence[Step]) -> dict[str, int]:
    """Counts for the preview line. Figures, not intentions."""
    out = {"create": 0, "adopt": 0, "ok": 0}
    for s in steps:
        out[s.action] += 1
    return out


def is_complete(guild_id: int, live_ids: Iterable[int], conn=None) -> bool:
    """True when every desired item has a stored id that still exists.

    The boot self-check uses this to decide between 'ready' and 'run /setup',
    and it deliberately checks liveness rather than mere presence -- a row
    pointing at a deleted channel is worse than no row, because it looks
    configured right up until an alert silently goes nowhere.
    """
    have = set(int(i) for i in live_ids)
    known = stored(guild_id, conn)
    return all(int(known.get(d.key, 0)) in have for d in DESIRED)
