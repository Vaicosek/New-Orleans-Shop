"""A read-only mirror of another server's public market. CONTRACT.md sec 13.

Why this exists: the owner prices New Orleans by hand, and wants to see what
an item fetches on a market that already has volume before he sets a number.

Three things this module will never do, and the reasons, because each one is a
way this could quietly go wrong:

1. It never writes to `items`. Their prices are in a DIFFERENT CURRENCY on a
   DIFFERENT SERVER. `0.0157` there and `3 g` here are not two measurements of
   one thing, and any code that treated them as one would produce a confident
   wrong price. Only the shape is information -- what is dear there and cheap
   here -- and reading shape is a person's job.
2. It never fails a boot, a command, or a page. The feed is a convenience; the
   shop is the product. Every failure path here ends in a stored error string
   and a return, never a raise that reaches a caller.
3. It never hammers the source. One cycle is two requests (their page cap is
   500, their catalogue is ~765), it identifies itself honestly in the
   User-Agent rather than pretending to be a browser, and on 429 or 503 it
   stops the cycle where it stands instead of retrying. Their robots.txt
   allows `use=reference` for a general agent, which is exactly what this is;
   it is not a training crawler and must not behave like one.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

from . import db

SOURCE = "diplomaticamc"

# Configurable so the URL can change, or the whole feed be switched off, from
# the panel's Variables tab -- this host has no shell and a redeploy to change
# a hostname would be absurd.
BASE_URL = os.environ.get("NOLA_REFMARKET_URL", "https://market.diplomaticamc.com").rstrip("/")
ENABLED = os.environ.get("NOLA_REFMARKET_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

PAGE = 500          # their cap; asking for more silently returns 500
MAX_PAGES = 4       # hard stop, so a wrong `total` cannot loop forever
TIMEOUT = 20

USER_AGENT = (
    "NewOrleansShop/1.0 (+https://neworleansshop.org; "
    "reference pricing for a Minecraft shop; contact via the site)"
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def match_name(value: str) -> str:
    """Their catalogue and ours line up on this and nothing else.

    'SMOOTH_STONE', 'minecraft:smooth_stone' and our 'Smooth Stone' all become
    'smoothstone'. Deliberately crude: a fuzzy matcher that guessed would pair
    'Oak Log' with 'Oak Wood' and hand the owner a price for the wrong item,
    which is worse than no price at all. Anything that does not match exactly
    simply has no counterpart, and the page says so.
    """
    value = value.strip().lower()
    if ":" in value:
        value = value.split(":", 1)[1]
    return _NON_ALNUM.sub("", value)


def _row(item: dict[str, Any]) -> tuple | None:
    key = item.get("item_key")
    name = item.get("display_name") or item.get("material") or key
    if not key or not name:
        return None
    return (
        SOURCE, str(key), str(name), match_name(str(name)),
        item.get("market_price"), item.get("market_price_source"),
        item.get("best_ask_unit_price"), item.get("best_bid_unit_price"),
        item.get("total_stock"), item.get("total_demand"), item.get("volume_24h"),
    )


def store(items: Iterable[dict[str, Any]]) -> int:
    """Replace the mirror with what was just fetched.

    A whole-table replace, not an upsert, and inside one transaction: an item
    they DELISTED must disappear here too, or the page shows a price for
    something nobody sells any more and gives no sign it is stale.
    """
    rows = [r for r in (_row(i) for i in items) if r is not None]
    if not rows:
        return 0
    with db.db() as c:
        c.execute("DELETE FROM ref_market WHERE source = ?", (SOURCE,))
        c.executemany(
            "INSERT INTO ref_market (source, item_key, display_name, match_name, "
            "  price, price_source, best_ask, best_bid, stock, demand, volume_24h) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def record(*, rows: int, error: str | None) -> None:
    """Every attempt lands here, succeeded or not.

    `ok_at` moves only on a cycle that actually stored rows. That separation is
    the whole point: a feed that stopped working three days ago still has a
    full table, and without an `ok_at` distinct from `attempted_at` it looks
    identical to a healthy one.
    """
    with db.db() as c:
        c.execute(
            "INSERT INTO ref_market_runs (source, ok_at, attempted_at, rows, error) "
            "VALUES (?, CASE WHEN ? THEN datetime('now') END, datetime('now'), ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "  ok_at = CASE WHEN excluded.ok_at IS NOT NULL THEN excluded.ok_at "
            "               ELSE ref_market_runs.ok_at END, "
            "  attempted_at = excluded.attempted_at, "
            "  rows = excluded.rows, error = excluded.error",
            (SOURCE, 1 if error is None else 0, rows, error),
        )


def _who_blocked(body: str) -> str:
    """Name what refused us, so the next step is obvious from one log line.

    A Cloudflare challenge and an application-level "you need a key" look
    identical from the status code alone, and they need opposite responses:
    one is an edge setting their operator can change in a click, the other is
    a conversation about an API key.
    """
    text = (body or "")[:2000].lower()
    if "cloudflare" in text or "cf-ray" in text or "attention required" in text:
        return "Cloudflare at the edge (a bot-protection setting, not their app)"
    if "api key" in text or "unauthorized" in text or "token" in text:
        return "their application (it wants credentials)"
    return "the server (no reason given)"


async def pull() -> tuple[int, str | None]:
    """One cycle. Returns (rows stored, error or None). Never raises.

    aiohttp rather than urllib because discord.py already depends on it and
    this runs inside the bot's event loop -- a blocking fetch here would stall
    every slash command for as long as the other server takes to answer.
    """
    if not ENABLED:
        return 0, "disabled by NOLA_REFMARKET_ENABLED"

    import aiohttp                                  # local: keeps import cost off boot

    items: list[dict[str, Any]] = []
    try:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            for page in range(MAX_PAGES):
                url = f"{BASE_URL}/api/market?limit={PAGE}&offset={page * PAGE}"
                async with session.get(url) as resp:
                    if resp.status in (429, 503):
                        # Their throttle. Stop the cycle here and keep whatever
                        # earlier pages gave us; do NOT retry. Six hours from
                        # now is soon enough and this is somebody else's server.
                        msg = f"HTTP {resp.status} from {SOURCE} -- backing off until next cycle"
                        if items:
                            stored = store(items)
                            record(rows=stored, error=msg)
                            return stored, msg
                        record(rows=0, error=msg)
                        return 0, msg
                    if resp.status in (401, 403):
                        # Their server saying no to this client. robots.txt
                        # grants `use=reference` to a general agent, but the
                        # live edge is the enforcement and it wins over the
                        # file. There is no fix here that is not evasion --
                        # spoofing a browser User-Agent to get past a 403 is
                        # exactly the behaviour the block exists to stop, and
                        # this project will not do it. The way through is to
                        # ask DiplomaticaMC's operator for access, not to
                        # dress up as something else. So: report it, plainly,
                        # and stop trying until someone changes something.
                        hint = _who_blocked(await resp.text())
                        msg = (f"HTTP {resp.status} from {SOURCE} -- blocked by {hint}. "
                               f"Not retrying and not spoofing a browser: ask their "
                               f"operator to allow this client, or set "
                               f"NOLA_REFMARKET_ENABLED=0.")
                        record(rows=0, error=msg)
                        return 0, msg
                    if resp.status != 200:
                        msg = f"HTTP {resp.status} from {SOURCE}"
                        record(rows=0, error=msg)
                        return 0, msg
                    payload = await resp.json()
                batch = payload.get("items") or []
                items.extend(batch)
                total = payload.get("total")
                if len(batch) < PAGE or (isinstance(total, int) and len(items) >= total):
                    break
    except Exception as err:                        # noqa: BLE001 -- a feed, not the shop
        msg = f"{type(err).__name__}: {err}"
        record(rows=0, error=msg)
        return 0, msg

    stored = store(items)
    record(rows=stored, error=None)
    return stored, None


def health() -> str:
    """One line for the boot self-check.

    Says NOT PULLED YET rather than OK when the table is full but no cycle has
    ever succeeded in this database -- reporting on the strength of rows that
    exist is how a dead feed passes its own health check.
    """
    if not ENABLED:
        return f"reference market: off (NOLA_REFMARKET_ENABLED)"
    try:
        with db.db() as c:
            run = c.execute("SELECT * FROM ref_market_runs WHERE source = ?", (SOURCE,)).fetchone()
            rows = c.execute("SELECT COUNT(*) n FROM ref_market WHERE source = ?",
                             (SOURCE,)).fetchone()["n"]
    except Exception as err:                        # noqa: BLE001
        return f"reference market: UNREADABLE -- {err}"
    if run is None or run["ok_at"] is None:
        tail = f" -- last error: {run['error']}" if run is not None and run["error"] else ""
        return f"reference market ({SOURCE}): NOT PULLED YET, {rows} rows held{tail}"
    line = f"reference market ({SOURCE}): last success {run['ok_at']} UTC, {rows} items"
    if run["error"]:
        line += f"  (last attempt failed: {run['error']})"
    return line


def compare(limit: int = 400) -> list[dict]:
    """Our catalogue with their figures beside it, for the staff view.

    LEFT JOIN from OUR items: this answers "what should I know about the
    things I sell", not "what is on their market". An item of ours they do not
    list is a real answer and appears with empty columns -- dropping it would
    turn "they have no price for this" into "this item does not exist".

    The join is done HERE, in Python, through `match_name` -- the same function
    that wrote the column. Writing the normalisation a second time in SQL would
    mean two rules that have to agree forever, and the day they stopped
    agreeing the symptom would be a silently empty column, not an error.
    """
    with db.db() as c:
        ours = c.execute(
            "SELECT name, price_coins, price_unit_pieces, stack_size "
            "  FROM items WHERE active = 1 ORDER BY name"
        ).fetchall()
        theirs = c.execute(
            "SELECT match_name, display_name, price, best_bid, stock, demand, volume_24h "
            "  FROM ref_market WHERE source = ?", (SOURCE,)
        ).fetchall()

    by_name: dict[str, dict] = {}
    for r in theirs:
        by_name.setdefault(r["match_name"], dict(r))

    out: list[dict] = []
    for i in ours:
        row = dict(i)
        ref = by_name.get(match_name(i["name"]))
        row["ref_name"] = ref["display_name"] if ref else None
        row["ref_price"] = ref["price"] if ref else None
        row["best_bid"] = ref["best_bid"] if ref else None
        row["ref_stock"] = ref["stock"] if ref else None
        row["ref_demand"] = ref["demand"] if ref else None
        row["volume_24h"] = ref["volume_24h"] if ref else None
        out.append(row)

    # Sorted by what somebody would act on: the things they are short of, most
    # wanted first, then everything else alphabetically.
    out.sort(key=lambda r: (r["ref_demand"] is None, -(r["ref_demand"] or 0), r["name"]))
    return out[:limit]
