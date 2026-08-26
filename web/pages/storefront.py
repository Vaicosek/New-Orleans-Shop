"""`/` and `/stock` -- public. No session, no dependency on the bot process.

Both routes read only the local database through `core.catalog`, so they
answer identically whether or not the Discord bot is currently running.
Every price is rendered through `pricing.price_label()` -- a bare number
never reaches this page.
"""
from __future__ import annotations

from aiohttp import web

from core.catalog import categories_with_items, get_stock, list_items
from core.pricing import price_label

from ..auth import resolve_identity
from ..shell import esc, page


async def storefront(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    # include_empty=False: a category nobody has stocked yet is not shown to
    # a customer, not even as a bare heading -- that is staff-only to-do
    # information (see /ledger).
    categories = categories_with_items(active_only=True, include_empty=False)

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
                rows = "".join(
                    f'<tr><td>{esc(i["name"])}</td>'
                    f'<td class="num">{esc(price_label(i["price_coins"], i["price_unit_pieces"], i["stack_size"]))}</td></tr>'
                    for i in g["items"]
                )
                subhead = ""
                if show_subheads:
                    # The owner's own slot bookkeeping ("logs - 12 slots") --
                    # shown only when there is a real total to show.
                    slot_note = f' <span class="dim">({g["slots"]:,} slots)</span>' if g["slots"] else ""
                    name = esc(g["subcategory"]) if g["subcategory"] else "Other"
                    subhead = f'<h4>{name}{slot_note}</h4>'
                group_html.append(
                    f'{subhead}'
                    '<div class="tablewrap sheet"><table><thead><tr>'
                    '<th>Item</th><th class="num">Price</th>'
                    f'</tr></thead><tbody>{rows}</tbody></table></div>'
                )
            sections.append(f'<h3>{esc(cat["name"])}</h3>' + "".join(group_html))
        table = "".join(sections)
    else:
        table = '<p class="empty">Nothing stocked yet.</p>'

    body = f"""
<h1>New Orleans</h1>
<p>Goods on offer today. See <a href="/stock">stock</a> for quantity on hand.</p>
<h2>Price sheet</h2>
{table}
"""
    return page("Storefront", "storefront", body, identity=identity)


async def stock(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    items = list_items(active_only=True)

    rows = []
    for i in items:
        s = get_stock(i["id"])
        rows.append(
            f'<tr><td>{esc(i["name"])}</td>'
            f'<td class="num">{esc(price_label(i["price_coins"], i["price_unit_pieces"], i["stack_size"]))}</td>'
            f'<td class="num">{s["pieces"]:,}</td>'
            f'<td class="num">{s["capacity"]:,}</td></tr>'
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
<h1>Stock</h1>
<p>Live quantity on hand, both price bases.</p>
{table}
"""
    return page("Stock", "stock", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/", storefront)
    app.router.add_get("/stock", stock)
