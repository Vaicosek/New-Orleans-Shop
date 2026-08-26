"""`/ledger` -- staff only. Balances, orders, payouts, the audit trail.

Anonymous visitors get 401; a signed-in non-staff visitor gets 403. Staff is
checked here, at the route, against the identity `auth.resolve_identity()`
already resolved -- never inferred from a query string or a form field.
Every query below reaches only `wallets`, `orders`, `order_claims`,
`ledger_entries` and `audit_actions`; nothing here touches anything
CONTRACT.md section 9 walls off.
"""
from __future__ import annotations

from aiohttp import web

from core.catalog import categories_with_items
from core.db import connection
from core.pricing import price_label

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


def _payouts() -> list[dict]:
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
        f'<tr><td>{esc(w["subject"])}</td><td class="num">{w["coins"]:,}</td>'
        f'<td>{"yes" if w["frozen"] else "no"}</td></tr>'
        for w in _wallets()
    ]
    order_rows = [
        f'<tr><td>#{o["id"]}</td><td>{esc(o["item_name"])}</td>'
        f'<td class="num">{o["requested_pieces"]:,}</td>'
        f'<td class="num">{o["produced_pieces"]:,}</td><td>{esc(o["status"])}</td>'
        f'<td class="num">{esc(price_label(o["price_coins"], o["price_unit_pieces"], o["stack_size"]))}</td></tr>'
        for o in _open_orders()
    ]
    payout_rows = [
        f'<tr><td>{esc(p["ts"])}</td><td>{esc(p["subject"])}</td>'
        f'<td class="num">{p["delta"]:+,}</td><td>{esc(p["reason"])}</td></tr>'
        for p in _payouts()
    ]
    audit_rows = [
        f'<tr><td>{esc(a["ts"])}</td><td>{esc(a["actor"])}</td>'
        f'<td>{esc(a["kind"])}</td><td>{esc(a["summary"])}</td>'
        f'<td class="num">{a["money_coins"]:,}</td></tr>'
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
<p>Internal. Balances, orders, payouts, and the audit trail.</p>
<h2>Catalog by category</h2>
<p>Every declared category, including planned ones with nothing stocked yet.</p>
{catalog_table}
<h2>Balances</h2>
{_table(["Subject", "Coins", "Frozen"], {1}, wallet_rows, "No wallets yet.")}
<h2>Open orders</h2>
{_table(["Order", "Item", "Requested", "Produced", "Status", "Price"], {2, 3, 5},
        order_rows, "No open orders.")}
<h2>Payouts</h2>
{_table(["When", "Subject", "Amount", "Reason"], {2}, payout_rows, "No payouts yet.")}
<h2>Audit trail</h2>
{_table(["When", "Actor", "Kind", "Summary", "Coins"], {4}, audit_rows, "No audit entries yet.")}
"""
    return page("Ledger", "ledger", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/ledger", ledger)
