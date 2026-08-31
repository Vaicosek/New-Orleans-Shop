"""`/me` -- a signed-in customer's own balance, orders, and history.

Anonymous visitors get 401, not a redirect: the status code says "sign in
required" on its own, for a script or a returning tab that never renders
the body. "Own orders" means the order claims this Discord user holds as a
worker -- what they claimed, delivered, and were paid -- read straight off
`order_claims`/`orders`, joined for the item name. Nothing here reaches past
`core.money`/`core.db` into anything CONTRACT.md section 9 walls off.
"""
from __future__ import annotations

from aiohttp import web

from core.db import connection
from core.money import balance, public_history
from core.pricing import money_text, price_label

from ..auth import resolve_identity
from ..shell import esc, page


# Internal status values are database vocabulary, not English. A page a
# customer reads says what happened; "awaiting_verification" says what the
# column is called. Real words anywhere a user looks.
STATUS_WORDS = {
    "open": "Open",
    "claimed": "Claimed",
    "awaiting_verification": "Awaiting approval",
    "fulfilled": "Paid",
    "cancelled": "Cancelled",
}


def status_word(status: str) -> str:
    return STATUS_WORDS.get(status, status.replace("_", " ").capitalize())


def _visible_history(subject: str, limit: int = 30) -> list[dict]:
    """Ledger rows this page may show.

    `core.money.public_history` decides what that means -- see CONTRACT.md
    sections 1 and 9. This page does not re-implement the rule and does not
    filter after fetching; it asks core for the public view.
    """
    return public_history(subject, limit=limit)


def _my_claims(subject: str) -> list[dict]:
    rows = connection().execute(
        "SELECT oc.pieces, oc.delivered, oc.paid_coins, o.id AS order_id, "
        "       o.status, o.price_coins, o.price_unit_pieces, o.stack_size, i.name AS item_name "
        "  FROM order_claims oc "
        "  JOIN orders o ON o.id = oc.order_id "
        "  JOIN items i ON i.id = o.item_id "
        " WHERE oc.worker = ? "
        " ORDER BY oc.claimed_at DESC LIMIT 50",
        (subject,),
    ).fetchall()
    return [dict(r) for r in rows]


def _claim_row(c: dict) -> str:
    # An unpaid claim shows an em-dash, not a zero -- "not paid yet" and
    # "paid nothing" are different facts. Every figure that IS money goes
    # through `pricing.money_text`, so it carries the `g` symbol after the
    # number; a bare number here is an ambiguous unit (CONTRACT.md sec 5).
    paid = money_text(c["paid_coins"]) if c["paid_coins"] is not None else "—"
    price = esc(price_label(c["price_coins"], c["price_unit_pieces"], c["stack_size"]))
    return (
        f'<tr><td>#{c["order_id"]}</td><td>{esc(c["item_name"])}</td>'
        f'<td class="num">{price}</td>'
        f'<td class="num">{c["pieces"]:,}</td><td class="num">{c["delivered"]:,}</td>'
        f'<td>{esc(status_word(c["status"]))}</td><td class="num">{paid}</td></tr>'
    )


def _history_row(e: dict) -> str:
    tone = "gain" if e["delta"] > 0 else "loss"
    return (
        f'<tr><td>{esc(e["ts"])}</td><td>{esc(e["reason"])}</td>'
        f'<td class="num {tone}">{money_text(e["delta"], sign=True)}</td>'
        f'<td class="num">{money_text(e["balance_after"])}</td></tr>'
    )



def _today(subject: str, staff: bool) -> list[tuple[str, str]]:
    """What is waiting, and who can act on it. The one section that makes
    this a hub rather than a statement.

    Ordered by whose move it is: things only THIS person can move come first,
    then things they are waiting on someone else for, then what is open to
    anyone. A hub that leads with other people's work is a noticeboard.

    Every row names what is waiting in plain words and says who can act. It
    does NOT offer a button: claiming and delivering happen in Discord, and a
    link here that looked actionable and was not would be worse than no link.
    """
    c = connection()
    rows: list[tuple[str, str]] = []

    mine = c.execute(
        "SELECT o.id, i.name AS item, oc.pieces, oc.delivered, "
        "       o.price_coins, o.price_unit_pieces, o.stack_size "
        "  FROM order_claims oc "
        "  JOIN orders o ON o.id = oc.order_id "
        "  JOIN items i ON i.id = o.item_id "
        " WHERE oc.worker = ? AND oc.delivered < oc.pieces "
        "   AND o.status IN ('open','claimed') "
        " ORDER BY oc.claimed_at",
        (subject,),
    ).fetchall()
    for r in mine:
        outstanding = r["pieces"] - r["delivered"]
        price = price_label(r["price_coins"], r["price_unit_pieces"], r["stack_size"])
        rows.append((
            f'#{r["id"]} {esc(r["item"])} &mdash; {outstanding:,} pieces still to deliver, '
            f'at {esc(price)}', "You"))

    waiting = c.execute(
        "SELECT o.id, i.name AS item, oc.delivered "
        "  FROM order_claims oc "
        "  JOIN orders o ON o.id = oc.order_id "
        "  JOIN items i ON i.id = o.item_id "
        " WHERE oc.worker = ? AND o.status = 'awaiting_verification' "
        " ORDER BY o.id",
        (subject,),
    ).fetchall()
    for r in waiting:
        rows.append((
            f'#{r["id"]} {esc(r["item"])} &mdash; {r["delivered"]:,} pieces delivered, '
            f'waiting to be approved', "Staff"))

    if staff:
        queue = c.execute(
            "SELECT o.id, i.name AS item, o.produced_pieces "
            "  FROM orders o JOIN items i ON i.id = o.item_id "
            " WHERE o.status = 'awaiting_verification' ORDER BY o.id"
        ).fetchall()
        for r in queue:
            rows.append((
                f'#{r["id"]} {esc(r["item"])} &mdash; {r["produced_pieces"]:,} pieces '
                f'delivered, needs approval', "You, as staff"))

    openable = c.execute(
        "SELECT o.id, i.name AS item, o.requested_pieces, o.produced_pieces, "
        "       o.price_coins, o.price_unit_pieces, o.stack_size "
        "  FROM orders o JOIN items i ON i.id = o.item_id "
        " WHERE o.status = 'open' ORDER BY o.id LIMIT 10"
    ).fetchall()
    for r in openable:
        remaining = r["requested_pieces"] - r["produced_pieces"]
        price = price_label(r["price_coins"], r["price_unit_pieces"], r["stack_size"])
        rows.append((
            f'#{r["id"]} {esc(r["item"])} &mdash; {remaining:,} pieces wanted, '
            f'at {esc(price)}', "Open to all"))

    return rows


async def me(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        body = (
            "<h1>Hub</h1>"
            "<p>Sign in with Discord to see your balance and orders.</p>"
            '<p><a href="/login">Sign in with Discord</a></p>'
        )
        return page("Hub", "account", body, status=401)

    bal = balance(identity.subject)
    claims = _my_claims(identity.subject)
    entries = _visible_history(identity.subject, limit=30)

    if claims:
        claims_table = (
            '<div class="tablewrap"><table><thead><tr>'
            '<th>Order</th><th>Item</th><th class="num">Price</th>'
            '<th class="num">Claimed</th><th class="num">Delivered</th>'
            '<th>Status</th><th class="num">Paid</th>'
            '</tr></thead><tbody>'
            + "".join(_claim_row(c) for c in claims) + '</tbody></table></div>'
        )
    else:
        claims_table = '<p class="empty">No orders claimed yet.</p>'

    if entries:
        history_table = (
            '<div class="tablewrap"><table><thead><tr>'
            '<th>When</th><th>Reason</th>'
            '<th class="num">Amount</th><th class="num">Balance</th>'
            '</tr></thead><tbody>'
            + "".join(_history_row(e) for e in entries) + '</tbody></table></div>'
        )
    else:
        history_table = '<p class="empty">No activity yet.</p>'

    waiting = _today(identity.subject, bool(getattr(identity, "staff", False)))
    if waiting:
        today_table = (
            '<div class="tablewrap"><table><thead><tr>'
            '<th>What is waiting</th><th>Who can act</th>'
            '</tr></thead><tbody>'
            + "".join(f'<tr><td>{what}</td><td class="dim">{esc(who)}</td></tr>'
                      for what, who in waiting)
            + '</tbody></table></div>'
        )
    else:
        # Empty means empty. One muted line, no placeholder rows.
        today_table = '<p class="empty">Nothing waiting on you.</p>'

    body = f"""
<h1>Hub</h1>

<h2>Today</h2>
{today_table}

<h2>Your money</h2>
<div class="sums">
  <div class="row"><span>Available to spend</span><span>{money_text(bal.available)}</span></div>
  <div class="row"><span>Held</span><span>{money_text(bal.held)}</span></div>
  <div class="row total"><span>Balance</span><span>{money_text(bal.coins)}</span></div>
</div>

<h2>Your orders</h2>
{claims_table}

<h2>History</h2>
{history_table}
"""
    return page("Hub", "account", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/me", me)
