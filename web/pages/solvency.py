"""`/solvency` -- staff only. Can the shop pay what it owes?

Every other staff page answers "what happened". This one answers the
question nobody could ask before it existed, and the reason it exists is
that the answer was NO and nothing said so: the shop sold goods for 320 and
paid 375 to have them made, losing 17% on every order it completed, for as
long as the two prices were one column. A per-unit loss is invisible in a
balance that somebody keeps topping up, and it is invisible in a ledger that
only ever lists individual movements. It shows up here, in one line, or it
shows up eventually as an empty treasury nobody can explain.

The shop's coins are NOT the shop's money. A player's balance is a debt: the
shop is holding gold that a player can spend or withdraw at any moment, and
escrowed bids are the same thing with a lock on them. So "are we solvent"
means treasury assets against everything owed to other people, and the
interesting number is the difference, not either side.

Read-only and derived on every request, never cached and never a stored
total -- the same discipline as the loyalty score and the team standings,
for the same reason: a cached figure and the ledger disagree the first time
anything is voided, and the cached one is always the one that is wrong.

Nothing under `bot/` is imported: `web/` is a separate process with no
gateway connection, and the section 9 wall scans this directory's imports.
"""
from __future__ import annotations

from aiohttp import web

from core.db import db_in
from core.pricing import money_text, price_label

from ..auth import resolve_identity
from ..shell import esc, page


def _one(c, sql: str, params: tuple = ()) -> int:
    row = c.execute(sql, params).fetchone()
    return int(row[0] or 0)


def solvency_figures() -> dict:
    """Assets, liabilities and the gap between them, all derived live.

    `unpaid_wages` is the one figure that has to be COMPUTED rather than
    summed off a column: delivered-but-unapproved work has no `paid_coins`
    yet (that column is written when the money actually moves), so what the
    shop owes for it is the order's own snapshotted wage rate applied to the
    actually delivered. Using the sell price here would overstate the debt
    by the whole margin; using `paid_coins` would report zero, which is the
    trap -- work is owed for the moment it is delivered, not the moment
    somebody gets to approving it.
    """
    with db_in() as c:
        held = _one(c, "SELECT COALESCE(SUM(coins),0) FROM wallets WHERE subject LIKE 'u:%'")
        treasury = _one(c, "SELECT COALESCE(SUM(coins),0) FROM wallets "
                            "WHERE subject LIKE 'treasury:%'")
        escrow = _one(c, "SELECT COALESCE(SUM(amount - captured - released),0) "
                          "FROM ledger_holds WHERE state = 'open'")
        loans_out = _one(c, "SELECT COALESCE(SUM(principal + interest - paid),0) "
                              "FROM loans WHERE status = 'open'")
        bonds_out = _one(c, "SELECT COALESCE(SUM(bh.units * b.unit_price),0) "
                              "FROM bond_holdings bh JOIN bonds b ON b.id = bh.bond_id "
                              "WHERE b.status = 'open'")
        wage_rows = c.execute(
            "SELECT oc.delivered, o.payout_coins, o.price_coins, o.price_unit_pieces "
            "  FROM order_claims oc JOIN orders o ON o.id = oc.order_id "
            " WHERE o.status = 'awaiting_verification' AND oc.paid_event IS NULL"
        ).fetchall()

    unpaid_wages = 0
    for row in wage_rows:
        rate = row["payout_coins"] or row["price_coins"]
        unit = row["price_unit_pieces"] or 1
        unpaid_wages += (int(row["delivered"]) * int(rate)) // int(unit)

    owed = held + unpaid_wages + bonds_out
    return {
        "treasury": treasury,
        "loans_out": loans_out,
        "assets": treasury + loans_out,
        "held": held,
        "escrow": escrow,
        "unpaid_wages": unpaid_wages,
        "bonds_out": bonds_out,
        "owed": owed,
        "gap": treasury - owed,
        "wage_claims": len(wage_rows),
    }


def margin_rows() -> list[dict]:
    """Per item: what it sells for against what it costs to have made.

    This is the table the margin bug would have died in. A negative row is
    an item the shop loses money on every single time somebody orders it,
    and no funding fixes that -- it just delays it.
    """
    with db_in() as c:
        rows = c.execute(
            "SELECT name, price_coins, price_unit_pieces, stack_size "
            "  FROM items WHERE active = 1 ORDER BY name"
        ).fetchall()
    from core.orders import worker_payout_for
    out = []
    for r in rows:
        sell = int(r["price_coins"])
        pay = worker_payout_for(sell)
        out.append({
            "name": r["name"], "sell": sell, "pay": pay, "margin": sell - pay,
            "unit_pieces": r["price_unit_pieces"], "stack_size": r["stack_size"],
            "pct": ((sell - pay) * 100 + sell // 2) // sell if sell else 0,
        })
    return sorted(out, key=lambda x: x["pct"])


async def solvency(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        return page("Solvency", "solvency",
                    "<h1>Solvency</h1><p>Sign in with Discord to continue.</p>",
                    status=401)
    if not identity.staff:
        return page("Solvency", "solvency",
                    "<h1>Solvency</h1><p>Staff only.</p>",
                    identity=identity, status=403)

    f = solvency_figures()
    covered = f["gap"] >= 0
    verdict_tone = "s-done" if covered else "s-stop"
    verdict = ("The treasury covers everything owed."
               if covered else
               "The treasury does NOT cover what is owed.")

    wages_note = (f' across {f["wage_claims"]} unapproved claim'
                  f'{"s" if f["wage_claims"] != 1 else ""}') if f["wage_claims"] else ""

    margins = margin_rows()
    loss_makers = [m for m in margins if m["margin"] <= 0]
    margin_rows_html = "".join(
        f'<tr><td>{esc(m["name"])}</td>'
        f'<td class="num">{esc(price_label(m["sell"], m["unit_pieces"], m["stack_size"]))}</td>'
        f'<td class="num">{esc(price_label(m["pay"], m["unit_pieces"], m["stack_size"]))}</td>'
        f'<td class="num {"s-stop" if m["margin"] <= 0 else "s-done"}">'
        f'{esc(money_text(m["margin"]))} &middot; {m["pct"]}%</td></tr>'
        for m in margins[:40]
    )
    margin_table = (
        f'<div class="tablewrap"><table><thead><tr><th>Item</th><th>Sells for</th>'
        f'<th>Pays the worker</th><th>Margin</th></tr></thead>'
        f'<tbody>{margin_rows_html}</tbody></table></div>'
        if margins else '<p class="empty">No active items.</p>'
    )
    loss_note = (
        f'<p class="notice">{len(loss_makers)} active item'
        f'{"s" if len(loss_makers) != 1 else ""} pay out as much as or more than '
        f'{"they sell" if len(loss_makers) != 1 else "it sells"} for. Every order '
        f'for {"one of those" if len(loss_makers) != 1 else "it"} costs the shop money.</p>'
        if loss_makers else ""
    )

    body = f"""
<div class="hero">
<h1>Solvency</h1>
<p>What the shop holds against what it owes. A player's balance is not the shop's
money &mdash; it is gold somebody can spend or withdraw at any moment.</p>
</div>

<h2 class="{verdict_tone}">{esc(verdict)}</h2>
<div class="sums">
<div class="row"><span>Treasury balances</span>
  <span class="num">{esc(money_text(f["treasury"]))}</span></div>
<div class="row"><span>Lent out, still owed to the shop</span>
  <span class="num">{esc(money_text(f["loans_out"]))}</span></div>
<div class="row total"><span>Assets</span>
  <span class="num">{esc(money_text(f["assets"]))}</span></div>
</div>

<div class="sums">
<div class="row"><span>Held for players</span>
  <span class="num">{esc(money_text(f["held"]))}</span></div>
<div class="row"><span>Unpaid wages{esc(wages_note)}</span>
  <span class="num">{esc(money_text(f["unpaid_wages"]))}</span></div>
<div class="row"><span>Bond principal outstanding</span>
  <span class="num">{esc(money_text(f["bonds_out"]))}</span></div>
<div class="row total"><span>Owed to other people</span>
  <span class="num">{esc(money_text(f["owed"]))}</span></div>
</div>

<div class="sums">
<div class="row total"><span>Treasury minus what is owed</span>
  <span class="num {verdict_tone}">{esc(money_text(f["gap"]))}</span></div>
<div class="row"><span>Escrowed in open bids (already inside the balances above)</span>
  <span class="num dim">{esc(money_text(f["escrow"]))}</span></div>
</div>

<h2>Margin per item</h2>
<p class="dim">What each item sells for against what the shop pays to have it made.
Worst first.</p>
{loss_note}
{margin_table}
"""
    return page("Solvency", "solvency", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/solvency", solvency)
