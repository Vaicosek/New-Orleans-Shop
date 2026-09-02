"""`/banking` -- a signed-in customer's whole money picture in one place.

The pieces existed and were scattered: a balance on `/me`, loans reachable
only from Discord's `/wallet`, bonds visible only on their own card in a
Discord channel. Somebody wanting to know "what do I have, what do I owe,
and what is owed to me" had to visit three surfaces and add it up
themselves, which is the same thing as not being able to find out.

READ-ONLY, deliberately. `/order` is this site's one write route
(CONTRACT.md section 12): borrowing, repaying and buying bonds all move
real money, and those paths are single-surfaced on Discord where the
preview-then-confirm gates live. Duplicating a money-moving flow here would
mean two implementations of the same irreversible step, which is how they
drift. So this page shows the numbers and says where each action lives.

Every figure is derived on read. Nothing is cached, and `held` in
particular is summed live from open holds rather than stored -- an escrowed
auction bid is still the player's gold, it just cannot be spent twice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from aiohttp import web

from core.db import db_in
from core.loans import credit_limit_for, outstanding_owed
from core.money import balance
from core.pricing import money_text

from ..auth import resolve_identity
from ..shell import esc, page


def _parse(ts: object) -> Optional[datetime]:
    """Naive UTC strings throughout this database. Stamp the zone on before
    comparing or every "due in N days" is out by the server's offset."""
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _due_text(when: Optional[datetime]) -> str:
    if when is None:
        return ""
    days = (when - datetime.now(timezone.utc)).days
    if days < 0:
        return f"overdue by {abs(days)} day{'s' if abs(days) != 1 else ''}"
    if days == 0:
        return "due today"
    return f"due in {days} day{'s' if days != 1 else ''}"


def _loans(subject: str) -> list[dict]:
    with db_in() as c:
        rows = c.execute(
            "SELECT id, principal, interest, paid, due_at, issued_at FROM loans "
            " WHERE subject = ? AND status = 'open' ORDER BY due_at ASC",
            (subject,),
        ).fetchall()
    return [dict(r) for r in rows]


def _bonds(subject: str) -> list[dict]:
    """Bonds this player holds units of, with what each is worth and when it
    pays. Only open series: a matured or voided bond has already settled and
    belongs in history, not in a holdings list."""
    with db_in() as c:
        rows = c.execute(
            "SELECT b.id, b.name, b.unit_price, b.coupon_bps, b.coupon_interval_days, "
            "       b.matures_at, b.next_coupon_at, h.units "
            "  FROM bond_holdings h JOIN bonds b ON b.id = h.bond_id "
            " WHERE h.subject = ? AND b.status = 'open' "
            " ORDER BY b.matures_at ASC",
            (subject,),
        ).fetchall()
    return [dict(r) for r in rows]


async def banking(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    if identity is None:
        return page("Banking", "banking",
                    "<h1>Banking</h1><p>Sign in with Discord to continue.</p>",
                    status=401)

    subject = identity.subject
    bal = balance(subject)
    owed = outstanding_owed(subject)
    limit = credit_limit_for(subject)
    loans = _loans(subject)
    bonds = _bonds(subject)

    bond_principal = sum(int(b["units"]) * int(b["unit_price"]) for b in bonds)
    # What the player is worth here: spendable gold, plus money lent to the
    # shop through bonds, less what they owe it back.
    net = bal.coins + bond_principal - owed

    if loans:
        loan_rows = "".join(
            f'<tr><td>#{l["id"]}</td>'
            f'<td class="num">{esc(money_text(l["principal"] + l["interest"] - l["paid"]))}</td>'
            f'<td class="num dim">{esc(money_text(l["principal"]))} + '
            f'{esc(money_text(l["interest"]))} interest</td>'
            f'<td class="{"s-stop" if (_parse(l["due_at"]) or datetime.now(timezone.utc)) < datetime.now(timezone.utc) else "s-wait"}">'
            f'{esc(_due_text(_parse(l["due_at"])))}</td></tr>'
            for l in loans
        )
        loans_html = (f'<div class="tablewrap"><table><thead><tr><th>Loan</th>'
                      f'<th>Still owed</th><th>Made up of</th><th>When</th></tr></thead>'
                      f'<tbody>{loan_rows}</tbody></table></div>')
    else:
        loans_html = '<p class="empty">Nothing borrowed.</p>'

    if bonds:
        bond_rows = "".join(
            f'<tr><td>{esc(b["name"])}</td>'
            f'<td class="num">{b["units"]:,}</td>'
            f'<td class="num">{esc(money_text(int(b["units"]) * int(b["unit_price"])))}</td>'
            f'<td class="num dim">{b["coupon_bps"] / 100:g}% every {b["coupon_interval_days"]} '
            f'day{"s" if b["coupon_interval_days"] != 1 else ""}</td>'
            f'<td class="dim">{esc(_due_text(_parse(b["matures_at"])))}</td></tr>'
            for b in bonds
        )
        bonds_html = (f'<div class="tablewrap"><table><thead><tr><th>Bond</th><th>Units</th>'
                      f'<th>Principal</th><th>Pays</th><th>Matures</th></tr></thead>'
                      f'<tbody>{bond_rows}</tbody></table></div>')
    else:
        bonds_html = '<p class="empty">No bonds held.</p>'

    headroom = max(limit - owed, 0)

    body = f"""
<div class="hero">
<h1>Banking</h1>
<p>Everything you hold, everything you owe, and everything owed to you.
Moving money &mdash; borrowing, repaying, buying bonds, sending gold &mdash; happens on
Discord's <code>/wallet</code> command and on each bond's own card, where the
confirmation steps are.</p>
</div>

<h2>Your gold</h2>
<div class="sums">
<div class="row"><span>Balance</span>
  <span class="num">{esc(money_text(bal.coins))}</span></div>
<div class="row"><span>Held in open bids</span>
  <span class="num dim">{esc(money_text(bal.held))}</span></div>
<div class="row"><span>Free to spend</span>
  <span class="num">{esc(money_text(bal.available))}</span></div>
<div class="row total"><span>Yours, all in</span>
  <span class="num">{esc(money_text(net))}</span></div>
</div>

<h2>Borrowing</h2>
<div class="sums">
<div class="row"><span>Owed to the shop</span>
  <span class="num">{esc(money_text(owed))}</span></div>
<div class="row"><span>Your limit, set by your rank</span>
  <span class="num dim">{esc(money_text(limit))}</span></div>
<div class="row"><span>Still available to borrow</span>
  <span class="num">{esc(money_text(headroom))}</span></div>
</div>
{loans_html}

<h2>Bonds you hold</h2>
{bonds_html}
"""
    return page("Banking", "banking", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/banking", banking)
