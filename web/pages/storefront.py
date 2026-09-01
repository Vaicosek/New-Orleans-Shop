"""`/` and `/inventory` -- public. No session, no dependency on the bot process.

Both routes read only the local database through `core.catalog`, so they
answer identically whether or not the Discord bot is currently running.
Every price is rendered through `pricing.price_label()` -- a bare number
never reaches this page.

`/order` is the one exception to "no session" above: it is the site's first
state-changing route (CONTRACT.md section 12), reachable only signed in. It
opens a production/restock request exactly the way Discord's shop panel
does -- `core.orders.create_order`, the same function, same rules, same
audit trail -- so nothing about what an order IS changes because it was
opened from a browser instead of Discord. No money moves here; that only
ever happens at `/orders` approval in Discord. The order is not pushed to
the orders channel from this process (the web process holds no live
Discord connection -- see `run_all.py`), so it surfaces to workers the same
way a card that failed to post already does: it exists and is claimable
from Discord's `/orders` command immediately.
"""
from __future__ import annotations

import secrets

from aiohttp import web

from core.catalog import CatalogError, categories_with_items, get_item, get_stock, list_items, quote
from core.orders import OrderError, create_order
from core.pricing import price_label

from ..auth import resolve_identity
from ..icons import icon_data_uri
from ..shell import esc, page


def _icon_html(item_name: str) -> str:
    uri = icon_data_uri(item_name)
    if uri:
        return f'<img class="icon" src="{uri}" alt="" width="40" height="40" loading="lazy">'
    # No mapped icon yet -- a monogram tile, not a broken image request or a
    # guessed texture. Same "real content or nothing" rule as everywhere
    # else on this page: a blank img tag would be a decorated absence.
    letter = esc(item_name[:1].upper()) if item_name else "?"
    return f'<div class="icon icon-fallback" aria-hidden="true">{letter}</div>'


def _order_form_html(item: dict, identity) -> str:
    if identity is None:
        return '<a class="order-link" href="/login">Sign in to order</a>'
    return f"""<form class="order-form" method="post" action="/order">
<input type="hidden" name="item_id" value="{item['id']}">
<input type="hidden" name="csrf" value="{esc(identity.csrf)}">
<input type="number" name="pieces" value="{item['stack_size']}" min="1" max="999999"
       inputmode="numeric" aria-label="Pieces to request">
<button type="submit">Request</button>
</form>"""


def _grid_html(items: list[dict], identity) -> str:
    cards = []
    for i in items:
        stock = get_stock(i["id"])
        price = esc(price_label(i["price_coins"], i["price_unit_pieces"], i["stack_size"]))
        cards.append(f"""<div class="item">
{_icon_html(i["name"])}
<div class="item-name">{esc(i["name"])}</div>
<div class="item-price">{price}</div>
<div class="item-stock dim">{stock["pieces"]:,} on hand</div>
{_order_form_html(i, identity)}
</div>""")
    return f'<div class="itemgrid">{"".join(cards)}</div>'


async def storefront(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    # include_empty=False: a category nobody has stocked yet is not shown to
    # a customer, not even as a bare heading -- that is staff-only to-do
    # information (see /ledger).
    categories = categories_with_items(active_only=True, include_empty=False)

    notice = ""
    ordered_id = request.query.get("ordered")
    if ordered_id and ordered_id.isdigit():
        notice = (f'<p class="notice">Order #{ordered_id} opened -- a worker can now '
                   f'claim it from Discord\'s <code>/orders</code>.</p>')

    if categories:
        sections = []
        for cat in categories:
            groups = cat["groups"]
            # A category with exactly one, unnamed sub-group is a category
            # that was never sub-grouped -- printing "None" (or a blank
            # heading) above it would be a redundant heading nobody wrote.
            show_subheads = not (len(groups) == 1 and not groups[0]["subcategory"])
            group_html = []
            for g in groups:
                subhead = ""
                if show_subheads:
                    # The owner's own slot bookkeeping ("logs - 12 slots") --
                    # shown only when there is a real total to show.
                    slot_note = f' <span class="dim">({g["slots"]:,} slots)</span>' if g["slots"] else ""
                    name = esc(g["subcategory"]) if g["subcategory"] else "Other"
                    subhead = f'<h4>{name}{slot_note}</h4>'
                group_html.append(f'{subhead}{_grid_html(g["items"], identity)}')
            sections.append(f'<h3>{esc(cat["name"])}</h3>' + "".join(group_html))
        table = "".join(sections)
    else:
        table = '<p class="empty">Nothing stocked yet.</p>'

    body = f"""
<div class="hero">
<h1>New Orleans</h1>
<p>Goods on offer today. See <a href="/inventory">inventory</a> for quantity on hand.</p>
</div>
{notice}
<h2>Price sheet</h2>
{table}
"""
    return page("Storefront", "storefront", body, identity=identity)


async def inventory(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    items = list_items(active_only=True)

    rows = []
    for i in items:
        s = get_stock(i["id"])
        # The one number on this page anybody decides on. Out is red, a
        # quarter or less of capacity is the gold that means somebody owes a
        # move, anything above that is plain. A shelf that is merely not full
        # is not news and gets no colour.
        if s["pieces"] <= 0:
            tone = " s-stop"
        elif s["capacity"] and s["pieces"] * 4 <= s["capacity"]:
            tone = " s-wait"
        else:
            tone = ""
        rows.append(
            f'<tr><td>{esc(i["name"])}</td>'
            f'<td class="num">{esc(price_label(i["price_coins"], i["price_unit_pieces"], i["stack_size"]))}</td>'
            f'<td class="num{tone}">{s["pieces"]:,}</td>'
            f'<td class="num dim">{s["capacity"]:,}</td></tr>'
        )

    if rows:
        table = (
            '<div class="tablewrap"><table><thead><tr>'
            '<th>Item</th><th class="num">Price</th>'
            '<th class="num">On hand</th><th class="num">Capacity</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        )
    else:
        table = '<p class="empty">Nothing stocked yet.</p>'

    body = f"""
<div class="hero">
<h1>Inventory</h1>
<p>Live quantity on hand, both price bases.</p>
</div>
{table}
"""
    return page("Inventory", "stock", body, identity=identity)


async def order_item(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        return web.Response(text="Sign in required.", status=401)

    form = await request.post()
    supplied_csrf = str(form.get("csrf", ""))
    if not secrets.compare_digest(supplied_csrf, identity.csrf or ""):
        return web.Response(text="Invalid or missing order token.", status=403)

    try:
        item_id = int(str(form.get("item_id", "")))
        pieces = int(str(form.get("pieces", "")))
        if pieces <= 0:
            raise ValueError
    except ValueError:
        return web.Response(text="Pieces must be a positive whole number.", status=400)

    try:
        get_item(item_id)  # rejects a stale/tampered item id before opening anything
        quote(item_id, pieces)  # same validation Discord's modal runs before create_order
        order_id = create_order(item_id, pieces, created_by=identity.subject)
    except (CatalogError, OrderError) as err:
        return web.Response(text=f"Could not open that order: {err}", status=400)

    return web.HTTPFound(f"/?ordered={order_id}")


def register(app: web.Application) -> None:
    app.router.add_get("/", storefront)
    app.router.add_get("/inventory", inventory)
    # /stock is what this page was called until the owner renamed it. It stays
    # registered because a URL somebody has already opened, bookmarked or
    # pasted into Discord is a promise, and a rename is not a reason to break
    # one. Same handler, not a redirect: a redirect would be one more thing
    # that can fail on a host with no shell.
    app.router.add_get("/stock", inventory)
    app.router.add_post("/order", order_item)
