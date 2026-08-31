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
        f'<td>{esc(c["status"])}</td><td class="num">{paid}</td></tr>'
    )


def _history_row(e: dict) -> str:
    tone = "gain" if e["delta"] > 0 else "loss"
    return (
        f'<tr><td>{esc(e["ts"])}</td><td>{esc(e["reason"])}</td>'
        f'<td class="num {tone}">{money_text(e["delta"], sign=True)}</td>'
        f'<td class="num">{money_text(e["balance_after"])}</td></tr>'
    )


async def me(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        body = (
            "<h1>Account</h1>"
            "<p>Sign in with Discord to see your balance and orders.</p>"
            '<p><a href="/login">Sign in with Discord</a></p>'
        )
        return page("Account", "account", body, status=401)

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

    body = f"""
<h1>Account</h1>
<p>{esc(identity.name)}</p>
<h2>Balance</h2>
<p>{money_text(bal.coins)} &middot; {money_text(bal.held)} held &middot; {money_text(bal.available)} available</p>
<h2>Your orders</h2>
{claims_table}
<h2>History</h2>
{history_table}
"""
    return page("Account", "account", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/me", me)
