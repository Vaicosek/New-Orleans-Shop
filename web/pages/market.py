"""`/auctions` and `/land` -- public, read-only.

Both routes read only the local database, like the storefront, so they
answer whether or not the Discord bot is running. They are a WINDOW on what
is currently for sale, never a place to act: bidding stays entirely on the
Discord card (CONTRACT.md sections 10 and 11a), because a bid places a real
escrow hold and that path is deliberately single-surfaced. So there is no
form on this page and no signed-in variant of it -- the call to action is
"bid from Discord", exactly as an order card's is.

Queries are local, plain SELECTs rather than an import of `bot.queries`:
`web/` is a separate process that must never import anything under `bot/`
(that pulls discord.py into a process with no gateway connection, and the
section 9 wall scans this directory's imports). They mirror
`bot/queries.py`'s leader read exactly -- the single `status = 'active'`
row -- so the site can never show a different leader than the one
settlement would actually pay.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from core.db import db_in
from core.pricing import money_text

from ..auth import resolve_identity
from ..shell import esc, page

LIVE_STATUSES = ("open", "closed")


def _mention_name(subject: str) -> str:
    """A leader is shown as a Discord id, not a name: this process holds no
    gateway connection, so it cannot resolve one, and inventing a display
    name from the subject string would be a guess. `u:` is stripped because
    the internal prefix is database vocabulary, not English."""
    if isinstance(subject, str) and subject.startswith("u:"):
        return subject.split(":", 1)[1]
    return str(subject)


def _closes_text(closes_at: str) -> str:
    """`closes_at` is a naive UTC string; stamp it UTC explicitly before
    comparing, or a naive datetime reads as local time and every countdown
    on the page is wrong by the server's offset."""
    try:
        dt = datetime.strptime(closes_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return esc(closes_at)
    delta = dt - datetime.now(timezone.utc)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        return "closed, awaiting settlement"
    if minutes < 60:
        return f"{minutes} min left"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h left"
    return f"{hours // 24}d left"


def live_auctions(limit: int = 50) -> list[dict]:
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    with db_in() as c:
        rows = c.execute(
            f"SELECT a.id, a.pieces, a.min_bid, a.min_increment, a.status, a.closes_at, "
            f"       i.name AS item_name "
            f"  FROM auctions a JOIN items i ON i.id = a.item_id "
            f" WHERE a.status IN ({placeholders}) "
            f" ORDER BY a.closes_at ASC LIMIT ?",
            (*LIVE_STATUSES, limit),
        ).fetchall()
        listings = []
        for row in rows:
            d = dict(row)
            leader = c.execute(
                "SELECT subject, amount FROM auction_bids "
                "WHERE auction_id = ? AND status = 'active' "
                "ORDER BY amount DESC, id ASC LIMIT 1",
                (d["id"],),
            ).fetchone()
            d["leader"] = leader["subject"] if leader else None
            d["leader_amount"] = leader["amount"] if leader else None
            listings.append(d)
    return listings


def live_land(limit: int = 50) -> list[dict]:
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    with db_in() as c:
        rows = c.execute(
            f"SELECT id, name, description, location, min_bid, min_increment, "
            f"       buy_now_price, status, closes_at "
            f"  FROM land_listings WHERE status IN ({placeholders}) "
            f" ORDER BY closes_at ASC LIMIT ?",
            (*LIVE_STATUSES, limit),
        ).fetchall()
        listings = []
        for row in rows:
            d = dict(row)
            leader = c.execute(
                "SELECT subject, amount FROM land_bids "
                "WHERE land_id = ? AND status = 'active' "
                "ORDER BY amount DESC, id ASC LIMIT 1",
                (d["id"],),
            ).fetchone()
            d["leader"] = leader["subject"] if leader else None
            d["leader_amount"] = leader["amount"] if leader else None
            listings.append(d)
    return listings


def _leader_cell(listing: dict) -> str:
    if listing["leader_amount"] is None:
        return f'<td class="num dim">no bids &middot; opens at {esc(money_text(listing["min_bid"]))}</td>'
    return (f'<td class="num s-done">{esc(money_text(listing["leader_amount"]))}'
            f' <span class="dim">&middot; {esc(_mention_name(listing["leader"]))}</span></td>')


async def auctions(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    listings = live_auctions()
    if listings:
        rows = "".join(
            f'<tr><td>{esc(l["item_name"])}</td>'
            f'<td class="num dim">{l["pieces"]:,}</td>'
            f'{_leader_cell(l)}'
            f'<td class="num dim">+{esc(money_text(l["min_increment"]))}</td>'
            f'<td class="dim">{esc(_closes_text(l["closes_at"]))}</td></tr>'
            for l in listings
        )
        table = (f'<table class="sheet"><thead><tr><th>Lot</th><th class="num">Pieces</th>'
                  f'<th class="num">Leading bid</th><th class="num">Min raise</th>'
                  f'<th>Closes</th></tr></thead><tbody>{rows}</tbody></table>')
    else:
        table = '<p class="empty">No lots open right now.</p>'

    body = f"""
<div class="hero">
<h1>Auctions</h1>
<p>Item lots New Orleans is selling. Bid from the <code>#auctions</code> channel in Discord --
the top bid when the clock runs out takes the lot.</p>
</div>
{table}
"""
    return page("Auctions", "auctions", body, identity=identity)


async def land(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    listings = live_land()
    if listings:
        cards = []
        for l in listings:
            where = f'<div class="dim">{esc(l["location"])}</div>' if l["location"] else ""
            desc = f'<p>{esc(l["description"])}</p>' if l["description"] else ""
            buy_now = (f'<div class="dim">Buy now: {esc(money_text(l["buy_now_price"]))}</div>'
                       if l["buy_now_price"] else "")
            if l["leader_amount"] is None:
                lead = f'<div class="dim">No bids &middot; opens at {esc(money_text(l["min_bid"]))}</div>'
            else:
                lead = (f'<div class="s-done">{esc(money_text(l["leader_amount"]))}'
                        f' <span class="dim">&middot; {esc(_mention_name(l["leader"]))}</span></div>')
            cards.append(f"""<div class="item">
<div class="item-name">{esc(l["name"])}</div>
{where}{desc}{lead}{buy_now}
<div class="dim">{esc(_closes_text(l["closes_at"]))}</div>
</div>""")
        table = f'<div class="itemgrid">{"".join(cards)}</div>'
    else:
        table = '<p class="empty">No plots listed right now.</p>'

    body = f"""
<div class="hero">
<h1>Land</h1>
<p>Plots for sale. Bid from the <code>#land</code> channel in Discord; a plot with a buy-now
price sells the instant a bid clears it.</p>
</div>
{table}
"""
    return page("Land", "land", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/auctions", auctions)
    app.router.add_get("/land", land)
