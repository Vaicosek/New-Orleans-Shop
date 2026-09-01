"""`/orders` -- the public work board. No session, no dependency on the bot.

The site could already show a signed-in visitor their OWN claims on `/me`,
but there was nowhere to see what work is actually going. This is that
page: the website's read-only window on what Discord's `/orders` panel
lists, so somebody deciding whether to join can see the work before they
have an account at all.

It is a WINDOW, never a place to act. Claiming an order places a real claim
row and starts a payable obligation, so that path is deliberately
single-surfaced on the Discord card (same reasoning as bidding in
`market.py`). There is no form here, no button, and no signed-in variant --
the call to action is "claim it from Discord's /orders".

Who claimed what is deliberately absent. The board is about the work; a
claimant's own record is on their `/me` page, and this process holds no
gateway connection to turn a Discord id into a name anyway.

Queries are local, plain SELECTs rather than an import of `bot.queries`:
`web/` is a separate process that must never import anything under `bot/`
(that pulls discord.py into a process with no gateway connection, and the
section 9 wall scans this directory's imports). The shape mirrors
`bot/queries.py::list_orders` so the board can never disagree with the
panel. Price comes off the ORDER row, not the item row: `orders` snapshots
`price_coins` / `price_unit_pieces` / `stack_size` at creation precisely so
that repricing an item cannot silently reprice work already in flight, and
reading the item's current price here would undo that.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from core.db import db_in
from core.pricing import price_label

from ..auth import resolve_identity
from ..shell import esc, page

# Still live: somebody can still act on it. A fulfilled or cancelled order
# is finished business and belongs to the people who were on it, not to a
# board about what is available now.
LIVE_STATUSES = ("open", "claimed", "awaiting_verification")

# Internal status values are database vocabulary, not English.
# "awaiting_verification" says what the column is called; a visitor reads
# what is actually happening.
STATUS_WORDS = {
    "open": "Open",
    "claimed": "Claimed",
    "awaiting_verification": "Awaiting approval",
}

# Colour by what the state asks of somebody, matching `/me`: gold when a
# move is owed, dim when it belongs to nobody in particular. An unknown
# status gets no colour rather than a guessed one.
STATUS_TONE = {
    "open": "s-open",
    "claimed": "s-wait",
    "awaiting_verification": "s-wait",
}


def status_word(status: str) -> str:
    return STATUS_WORDS.get(status, str(status).replace("_", " ").capitalize())


def _waiting_text(created_at: str) -> str:
    """How long the order has been sitting, in full words.

    `created_at` is a naive UTC string. Stamp it UTC explicitly BEFORE
    comparing or the naive datetime reads as local time and every figure on
    this page is wrong by the server's offset. An unparseable stamp falls
    back to printing the stamp itself -- a wrong duration would be a
    confident lie, the raw timestamp is merely ugly.
    """
    try:
        opened = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return f"Opened {esc(created_at)}"
    seconds = (datetime.now(timezone.utc) - opened).total_seconds()
    if seconds < 0:
        # A clock skew, not a fact about the order. Say the neutral thing.
        return "Opened just now"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "Waiting less than a minute"
    if minutes < 60:
        return f"Waiting {minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"Waiting {hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"Waiting {days} day{'s' if days != 1 else ''}"


def _progress_text(produced: int, requested: int) -> str:
    """Pieces made against pieces wanted, always with the unit spelled out.

    Zero produced says "none of the N pieces made yet" rather than "0 of N":
    "nobody has started" is the fact a worker is scanning for, and it should
    read as a sentence, not as a figure that has to be decoded.
    """
    if produced <= 0:
        return f"None of {requested:,} pieces made yet"
    if produced >= requested:
        return f"All {requested:,} pieces made"
    return f"{produced:,} of {requested:,} pieces made"


def live_orders(limit: int = 100) -> list[dict]:
    """Every order somebody can still act on, longest-waiting first.

    Oldest first, not newest: this page exists to answer "what needs
    doing", and the order that has been sitting longest is the one that
    most needs an answer.
    """
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    with db_in() as c:
        rows = c.execute(
            f"SELECT o.id, o.requested_pieces, o.produced_pieces, o.status, "
            f"       o.price_coins, o.price_unit_pieces, o.stack_size, o.created_at, "
            f"       i.name AS item_name "
            f"  FROM orders o JOIN items i ON i.id = o.item_id "
            f" WHERE o.status IN ({placeholders}) "
            f" ORDER BY o.created_at ASC, o.id ASC LIMIT ?",
            (*LIVE_STATUSES, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _order_card(o: dict) -> str:
    """One order as a card, not a table row.

    A card rather than a row because the price carries BOTH bases and that
    string is far too long for a cell -- in a table it would either be
    clipped or push the whole thing sideways off a phone. `itemgrid` drops
    to two columns under 560px on its own, so nothing here scrolls off.
    """
    price = esc(price_label(o["price_coins"], o["price_unit_pieces"], o["stack_size"]))
    tone = STATUS_TONE.get(o["status"], "")
    return f"""<div class="item">
<div class="item-name">{esc(o["item_name"])}</div>
<div class="item-stock dim">Order #{o["id"]}</div>
<div class="item-price">{price}</div>
<div>{esc(_progress_text(o["produced_pieces"], o["requested_pieces"]))}</div>
<div class="{tone}">{esc(status_word(o["status"]))}</div>
<div class="item-stock dim">{esc(_waiting_text(o["created_at"]))}</div>
</div>"""


async def orders(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)

    # The board is public, so a database that will not answer must still
    # produce a page. "We could not read the board" and "the board is
    # empty" are different facts and must never look the same -- an
    # exception rendered as the empty line would quietly tell every visitor
    # there is no work.
    try:
        listings = live_orders()
    except Exception:  # noqa: BLE001 -- the page still renders without the board
        body = """
<div class="hero">
<h1>Order board</h1>
<p>Work New Orleans is paying for right now.</p>
</div>
<p class="notice notice-loss">The order board could not be read just now. It is still
listed in Discord under <code>/orders</code>.</p>
"""
        return page("Order board", "orders", body, identity=identity, status=503)

    if listings:
        # Empty means empty, and full means full: one card per real order,
        # nothing padded out to fill the grid.
        board = f'<div class="itemgrid">{"".join(_order_card(o) for o in listings)}</div>'
    else:
        board = '<p class="empty">No orders open right now.</p>'

    body = f"""
<div class="hero">
<h1>Order board</h1>
<p>Work New Orleans is paying for right now, longest waiting first. Claim one from the
<code>/orders</code> command in Discord -- the pay shown is what the finished pieces earn.</p>
</div>
{board}
"""
    return page("Order board", "orders", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/orders", orders)
