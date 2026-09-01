"""`/ledger` -- staff only. Balances, orders, order settlements, the audit trail.

Anonymous visitors get 401; a signed-in non-staff visitor gets 403. Staff is
checked here, at the route, against the identity `auth.resolve_identity()`
already resolved -- never inferred from a query string or a form field.
Every query below reaches only `wallets`, `orders`, `order_claims`,
`ledger_entries` and `audit_actions`; nothing here touches anything
CONTRACT.md section 9 walls off.
"""
from __future__ import annotations

from aiohttp import web

from core import refmarket
from core.catalog import categories_with_items
from core.db import connection
from core.loyalty import effective_tier
from core import pricing
from core.pricing import money_text, price_label

from ..auth import resolve_identity
from ..shell import esc, page


def _wallets() -> list[dict]:
    rows = connection().execute(
        "SELECT subject, coins, frozen FROM wallets ORDER BY coins DESC LIMIT 200"
    ).fetchall()
    return [dict(r) for r in rows]


def _open_orders() -> list[dict]:
    rows = connection().execute(
        "SELECT o.id, i.name AS item_name, o.requested_pieces, o.produced_pieces, "
        "       o.status, o.price_coins, o.price_unit_pieces, o.stack_size "
        "  FROM orders o JOIN items i ON i.id = o.item_id "
        " WHERE o.status IN ('open', 'claimed', 'awaiting_verification') "
        " ORDER BY o.created_at DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


def _order_settlements() -> list[dict]:
    rows = connection().execute(
        "SELECT ts, subject, delta, reason FROM ledger_entries "
        " WHERE ref_kind = 'order' ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return [dict(r) for r in rows]


def _audit() -> list[dict]:
    rows = connection().execute(
        "SELECT ts, actor, kind, summary, money_coins FROM audit_actions "
        " ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return [dict(r) for r in rows]


def _num(value, places: int = 4) -> str:
    """A quoted foreign figure, or an em-dash.

    Em-dash, never 0: "they do not list this" and "they price it at nothing"
    are different facts and must not look the same. Trailing zeros are
    trimmed so a column of 0.0156 and 3 reads as numbers rather than as a
    fixed-width machine dump.
    """
    if value is None:
        return "&mdash;"
    if isinstance(value, int):
        return f"{value:,}"
    text = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _reference_market() -> str:
    """Our catalogue with the other market's figures beside it.

    Read-only, and it stays read-only: nothing on this page offers to apply a
    price. Their numbers are in another server's currency, so the absolute
    figures are not ours to copy -- what is worth reading is which of our
    items people over there are actually short of.
    """
    try:
        rows = refmarket.compare()
    except Exception as err:              # noqa: BLE001 -- a feed, not the ledger
        return f'<p class="empty">Reference market unavailable: {esc(err)}</p>'

    matched = [r for r in rows if r["ref_name"] is not None]
    if not matched:
        return ('<p class="empty">Nothing pulled yet, or none of our items appear '
                'on their market.</p>')

    body = "".join(
        f'<tr><td>{esc(r["name"])}</td>'
        f'<td class="num">{esc(price_label(r["price_coins"], r["price_unit_pieces"], r["stack_size"]))}</td>'
        f'<td class="num">{_num(r["ref_price"])}</td>'
        f'<td class="num">{_num(r["best_bid"])}</td>'
        f'<td class="num{" s-wait" if (r["ref_demand"] or 0) > 0 else ""}">{_num(r["ref_demand"], 0)}</td>'
        f'<td class="num">{_num(r["ref_stock"], 0)}</td>'
        f'<td class="num">{_num(r["volume_24h"], 0)}</td></tr>'
        for r in matched
    )
    unmatched = len(rows) - len(matched)
    tail = (f'<p class="empty">{unmatched:,} of our items do not appear on their '
            f'market.</p>' if unmatched else "")
    return (
        '<div class="tablewrap"><table><thead><tr>'
        '<th>Item</th><th class="num">Our price</th><th class="num">Their price</th>'
        '<th class="num">Their bid</th><th class="num">Wanted there</th>'
        '<th class="num">On offer there</th><th class="num">Traded 24h</th>'
        f'</tr></thead><tbody>{body}</tbody></table></div>{tail}'
    )


def _table(headers: list[str], nums: set[int], rows: list[str], empty_text: str) -> str:
    if not rows:
        return f'<p class="empty">{empty_text}</p>'
    head = "".join(
        f'<th class="num">{h}</th>' if idx in nums else f'<th>{h}</th>'
        for idx, h in enumerate(headers)
    )
    return (f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


async def ledger(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        return page("Ledger", "ledger",
                    "<h1>Ledger</h1><p>Sign in with Discord to continue.</p>",
                    status=401)
    if not identity.staff:
        return page("Ledger", "ledger",
                    "<h1>Ledger</h1><p>Staff only.</p>",
                    identity=identity, status=403)

    wallet_rows = [
        f'<tr><td>{esc(w["subject"])}</td><td class="num">{money_text(w["coins"])}</td>'
        f'<td>{"yes" if w["frozen"] else "no"}</td>'
        f'<td>{esc(effective_tier(w["subject"])["name"])}</td></tr>'
        for w in _wallets()
    ]
    order_rows = [
        f'<tr><td>#{o["id"]}</td><td>{esc(o["item_name"])}</td>'
        f'<td class="num">{o["requested_pieces"]:,}</td>'
        f'<td class="num">{o["produced_pieces"]:,}</td><td>{esc(o["status"])}</td>'
        f'<td class="num">{esc(price_label(o["price_coins"], o["price_unit_pieces"], o["stack_size"]))}</td></tr>'
        for o in _open_orders()
    ]
    settlement_rows = [
        f'<tr><td>{esc(p["ts"])}</td><td>{esc(p["subject"])}</td>'
        f'<td class="num">{money_text(p["delta"], sign=True)}</td>'
        f'<td>{esc(p["reason"])}</td></tr>'
        for p in _order_settlements()
    ]
    audit_rows = [
        f'<tr><td>{esc(a["ts"])}</td><td>{esc(a["actor"])}</td>'
        f'<td>{esc(a["kind"])}</td><td>{esc(a["summary"])}</td>'
        f'<td class="num">{money_text(a["money_coins"])}</td></tr>'
        for a in _audit()
    ]

    categories = categories_with_items(active_only=False, include_empty=True)
    catalog_sections = []
    for cat in categories:
        if cat["items"]:
            # Staff view: subcategory shown as its own column rather than
            # sub-headings, since this table also needs to show Status --
            # a staff reader wants to scan for "inactive" across the whole
            # category, not per sub-group.
            item_rows = "".join(
                f'<tr><td>{esc(i["name"])}</td>'
                f'<td>{esc(i["subcategory"]) if i["subcategory"] else ""}</td>'
                f'<td class="num">{esc(price_label(i["price_coins"], i["price_unit_pieces"], i["stack_size"]))}</td>'
                f'<td>{"active" if i["active"] else "inactive"}</td></tr>'
                for i in cat["items"]
            )
            catalog_sections.append(
                f'<h3>{esc(cat["name"])}</h3>'
                '<div class="tablewrap"><table><thead><tr>'
                '<th>Item</th><th>Subcategory</th><th class="num">Price</th><th>Status</th>'
                f'</tr></thead><tbody>{item_rows}</tbody></table></div>'
            )
        else:
            note = f' &mdash; {esc(cat["note"])}' if cat["note"] else ""
            catalog_sections.append(
                f'<h3>{esc(cat["name"])}</h3>'
                f'<p class="empty">Planned, no items yet{note}.</p>'
            )
    catalog_table = "".join(catalog_sections) if catalog_sections else '<p class="empty">No categories yet.</p>'

    body = f"""
<h1>Ledger</h1>
<p>Internal. Balances, orders, order settlements, and the audit trail.</p>
<h2>Catalog by category</h2>
<p>Every declared category, including planned ones with nothing stocked yet.</p>
{catalog_table}
<h2>Balances</h2>
{_table(["Subject", "Balance", "Frozen", "Rank"], {1}, wallet_rows, "No wallets yet.")}
<h2>Open orders</h2>
{_table(["Order", "Item", "Requested", "Produced", "Status", "Price"], {2, 3, 5},
        order_rows, "No open orders.")}
<h2>Order settlements</h2>
{_table(["When", "Subject", "Amount", "Reason"], {2}, settlement_rows, "No settlements yet.")}
<h2>Audit trail</h2>
{_table(["When", "Actor", "Kind", "Summary", "Amount"], {4}, audit_rows, "No audit entries yet.")}

<h2>Reference market</h2>
<p>Their prices are in their server's currency, not in {esc(pricing.CURRENCY)}
&mdash; the two are not the same unit and the figures do not convert. What reads
across is the shape: which of our items people over there are short of. Nothing
here changes a price.</p>
<p class="dim">{esc(refmarket.health())}</p>
{_reference_market()}
"""
    return page("Ledger", "ledger", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/ledger", ledger)
