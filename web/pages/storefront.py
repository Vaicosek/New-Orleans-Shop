"""`/` and `/inventory` -- public. No session, no dependency on the bot process.

Both routes read only the local database through `core.catalog`, so they
answer identically whether or not the Discord bot is currently running.
Every price is rendered through `pricing.price_label()` -- a bare number
never reaches this page.

`/order` is the one exception to "no session" above: it is the site's first
state-changing route (CONTRACT.md section 12), reachable only signed in. A
signed-in visitor checks off any number of items across the grid, types a
piece count into a plain number field for each (no JavaScript anywhere on
this site, so it is a bare `<input type=number>`, not a script-driven
control), and one submit opens a
production/restock request for every checked item in a single POST. Each
one goes through `core.orders.create_order` -- the same function, same
rules, same audit trail Discord's shop panel uses -- independently, so one
bad line (a stale item, a since-emptied field) never blocks the rest of the
batch; the redirect reports exactly how many actually opened. No money
moves here; that only ever happens at `/orders` approval in Discord. The
order is not pushed to the orders channel from this process (the web
process holds no live Discord connection -- see `run_all.py`), so it
surfaces to workers the same way a card that failed to post already does:
it exists and is claimable from Discord's `/orders` command immediately.
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


def _cart_controls_html(item: dict) -> str:
    """Checkbox, a quantity field, and the unit that quantity is counted in,
    inside the page's single cart form.

    No JavaScript: a plain `<input type=number>` and a plain `<select>`, so
    this works identically with scripting off.

    **The unit defaults to the one the price beside it is quoted in.** A log
    priced "1 g / stack of 64" is bought by the stack, and asking for it in
    pieces makes the buyer do the multiplication the price label already
    did for them -- typing 64 to mean one stack is a conversion, and a
    conversion is where a mis-order comes from. So when an item is quoted
    per stack (`price_unit_pieces == stack_size`) the field reads "1 stack";
    otherwise it stays in pieces, pre-filled with one stack's worth, exactly
    as before.

    An item that does not stack (`stack_size` of 1 -- a shulker, a saddle)
    gets no unit control at all rather than a dropdown whose two options
    mean the same thing. `_resolve_pieces` treats a missing unit as pieces,
    so the absent control is not a special case there either.
    """
    item_id = item["id"]
    stack = int(item["stack_size"] or 1)
    quoted_per_stack = stack > 1 and int(item["price_unit_pieces"] or 0) == stack
    default_qty = 1 if quoted_per_stack else stack

    if stack > 1:
        opts = "".join(
            f'<option value="{value}"{" selected" if value == default_unit else ""}>{label}</option>'
            for value, label, default_unit in (
                ("stacks", f"stacks of {stack}", "stacks" if quoted_per_stack else "pieces"),
                ("pieces", "pieces", "stacks" if quoted_per_stack else "pieces"),
            )
        )
        unit_html = (f'<label class="unit-field"><span class="vis-hidden">Unit</span>'
                     f'<select name="unit_{item_id}" aria-label="Unit">{opts}</select></label>')
    else:
        unit_html = '<span class="unit-fixed dim">pieces</span>'

    return f"""<div class="cart-controls">
<label class="cart-check"><input type="checkbox" name="items" value="{item_id}"> Select</label>
<div class="qty-row">
<label class="qty-field">Qty
<input type="number" name="qty_{item_id}" value="{default_qty}" min="1" max="999999"
       inputmode="numeric" aria-label="Quantity"></label>
{unit_html}
</div>
</div>"""


def _grid_html(items: list[dict], identity) -> str:
    cards = []
    for i in items:
        stock = get_stock(i["id"])
        price = esc(price_label(i["price_coins"], i["price_unit_pieces"], i["stack_size"]))
        controls = (_cart_controls_html(i) if identity is not None
                    else '<a class="order-link" href="/login">Sign in to order</a>')
        cards.append(f"""<div class="item">
{_icon_html(i["name"])}
<div class="item-name">{esc(i["name"])}</div>
<div class="item-price">{price}</div>
<div class="item-stock dim">{stock["pieces"]:,} on hand</div>
{controls}
</div>""")
    return f'<div class="itemgrid">{"".join(cards)}</div>'


def _cart_submit_html(identity) -> str:
    if identity is None:
        return ""
    return f"""<div class="cart-submit">
<input type="hidden" name="csrf" value="{esc(identity.csrf)}">
<button type="submit">Request selected items</button>
</div>"""


async def storefront(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    # include_empty=False: a category nobody has stocked yet is not shown to
    # a customer, not even as a bare heading -- that is staff-only to-do
    # information (see /ledger).
    categories = categories_with_items(active_only=True, include_empty=False)

    notice = ""
    ordered_raw = request.query.get("ordered", "")
    ordered_ids = [p for p in ordered_raw.split(",") if p.isdigit()]
    if ordered_ids:
        failed = request.query.get("failed", "")
        failed_note = ""
        if failed.isdigit() and int(failed) > 0:
            n = int(failed)
            failed_note = f' ({n} other{"s" if n != 1 else ""} could not be opened.)'
        label = "Order" if len(ordered_ids) == 1 else "Orders"
        ids_text = ", ".join(f"#{i}" for i in ordered_ids)
        notice = (f'<p class="notice">{label} {ids_text} opened -- a worker can now '
                   f'claim {"it" if len(ordered_ids) == 1 else "them"} from Discord\'s '
                   f'<code>/orders</code>.{failed_note}</p>')
    elif request.query.get("failed"):
        notice = '<p class="notice notice-loss">Could not open any of the selected orders.</p>'

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

    if categories and identity is not None:
        table = (f'<form method="post" action="/order">'
                  f'{_cart_submit_html(identity)}{table}{_cart_submit_html(identity)}</form>')

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


#: The largest order the form allows, in PIECES, matching the number input's
#: own `max`. Enforced here as well because `max` is a client-side hint and
#: nothing under `core/` bounds `requested_pieces` above zero -- so before
#: the unit selector existed, a hand-posted form could already open an order
#: for a billion pieces, and multiplying a stack count by `stack_size` would
#: have made that 64 times easier to do by accident.
MAX_ORDER_PIECES = 999_999


def _resolve_pieces(form, item_id: int, stack_size: int) -> int:
    """A checked item's quantity in PIECES -- the number typed, converted
    from whatever unit the visitor picked beside it.

    Pieces are the only unit anything downstream understands: `orders`
    stores `requested_pieces`, and `core/pricing.py` divides by
    `price_unit_pieces` alone. `stack_size` multiplies a COUNT here and
    never divides a price -- the two have been one column once before, and
    that is how saplings priced 1 g per 32 ended up half-priced.

    Raises ValueError for anything that is not a positive whole number
    within the form's own maximum, or for a unit that is not one this form
    offers -- the caller turns any of those into a per-item skip, never a
    whole-batch failure. A missing unit means pieces, so a non-stacking
    item (which renders no unit control at all) needs no special case.
    """
    raw = str(form.get(f"qty_{item_id}", "")).strip()
    count = int(raw)
    if count <= 0:
        raise ValueError("quantity must be positive")

    unit = str(form.get(f"unit_{item_id}", "pieces")).strip().lower() or "pieces"
    if unit not in ("pieces", "stacks"):
        raise ValueError(f"unknown unit {unit!r}")

    pieces = count * max(int(stack_size or 1), 1) if unit == "stacks" else count
    if pieces > MAX_ORDER_PIECES:
        raise ValueError(f"{pieces} pieces is over the {MAX_ORDER_PIECES} maximum")
    return pieces


async def order_item(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        return web.Response(text="Sign in required.", status=401)

    form = await request.post()
    supplied_csrf = str(form.get("csrf", ""))
    if not secrets.compare_digest(supplied_csrf, identity.csrf or ""):
        return web.Response(text="Invalid or missing order token.", status=403)

    checked = form.getall("items", [])
    if not checked:
        return web.Response(text="Select at least one item to request.", status=400)

    opened: list[int] = []
    failed = 0
    for raw_id in checked:
        try:
            item_id = int(str(raw_id))
        except ValueError:
            failed += 1
            continue
        # Resolved BEFORE the quantity: converting "3 stacks" into pieces
        # needs this item's own stack_size, and taking that from the catalog
        # rather than from the posted form means a tampered post cannot
        # inflate its own multiplier.
        try:
            item = get_item(item_id)  # rejects a stale/tampered item id before opening anything
        except CatalogError:
            failed += 1
            continue
        try:
            pieces = _resolve_pieces(form, item_id, item["stack_size"])
        except ValueError:
            failed += 1
            continue
        try:
            quote(item_id, pieces)  # same validation Discord's modal runs before create_order
            order_id = create_order(item_id, pieces, created_by=identity.subject)
        except (CatalogError, OrderError):
            failed += 1
            continue
        opened.append(order_id)

    if not opened:
        return web.Response(text="Could not open any of the selected orders.", status=400)

    query = f"ordered={','.join(str(i) for i in opened)}"
    if failed:
        query += f"&failed={failed}"
    return web.HTTPFound(f"/?{query}")


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
