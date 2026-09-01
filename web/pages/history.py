"""`/history` -- public. What has actually happened at the shop lately.

Read-only and session-free like the storefront: it opens the local database
and nothing else, so it answers whether or not the Discord bot is running.

Three real event sources merged into one reverse-chronological list --
orders that were paid out, item lots that sold, plots that sold. They live
in three unrelated tables with three different shapes, so each is read on
its own terms and normalised here rather than forced into one clever
UNION: the columns genuinely differ (an order has pieces and no winner, a
lot has a winner and no pieces), and a query that pretends otherwise
becomes unreadable the first time one of the three gains a column.

Commerce only. This page reads the order, auction and land tables and no
others; the tables section 9 walls off are named in
`tests/test_no_wagering_on_web.py`, which is also what enforces it. That
test scans this file as plain text, so the forbidden names are not written
here even in a comment.

Nothing under `bot/` is imported: `web/` is a separate process with no
gateway connection, so a wallet subject is shown as its bare Discord id
rather than a display name this process cannot resolve.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from aiohttp import web

from core.db import db_in
from core.pricing import money_text

from ..auth import resolve_identity
from ..shell import esc, page

ROW_LIMIT = 40


def _short_id(subject: object) -> str:
    """`u:` is internal database vocabulary, not English."""
    text = str(subject or "")
    if text.startswith("u:"):
        return text.split(":", 1)[1]
    return text


def _parse(ts: object) -> Optional[datetime]:
    """Every timestamp in this database is a naive UTC string. Stamp it UTC
    explicitly or a naive datetime is read as local time and every date on
    this page is wrong by the server's offset -- and near midnight, wrong by
    a day."""
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _when_text(when: Optional[datetime]) -> str:
    if when is None:
        return ""
    now = datetime.now(timezone.utc)
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return f"{when.day} {when.strftime('%B')} {when.year}"


def recent_events(limit: int = ROW_LIMIT) -> list[dict]:
    """The three sources, each already narrowed to what finished, merged and
    cut to `limit`. Each source is limited on its own first so one busy
    week of orders cannot push every lot off the page before the merge.

    An order's completion time is `closed_at`; a lot's and a plot's is
    `settled_at`. A row whose stamp will not parse still appears -- it sorts
    last rather than vanishing, because dropping a real event to keep the
    sort tidy is the worse trade.
    """
    events: list[dict] = []
    with db_in() as c:
        for row in c.execute(
            "SELECT o.id, o.requested_pieces, o.closed_at, i.name AS item_name "
            "  FROM orders o JOIN items i ON i.id = o.item_id "
            " WHERE o.status = 'fulfilled' "
            " ORDER BY o.closed_at DESC LIMIT ?", (limit,),
        ).fetchall():
            events.append({
                "kind": "order",
                "when": _parse(row["closed_at"]),
                "what": f'{row["requested_pieces"]:,} pieces of {row["item_name"]} delivered',
                "detail": "order paid",
                "tone": "s-done",
            })

        for row in c.execute(
            "SELECT a.id, a.pieces, a.winner, a.winning_amount, a.settled_at, "
            "       i.name AS item_name "
            "  FROM auctions a JOIN items i ON i.id = a.item_id "
            " WHERE a.status = 'settled' AND a.winner IS NOT NULL "
            " ORDER BY a.settled_at DESC LIMIT ?", (limit,),
        ).fetchall():
            events.append({
                "kind": "lot",
                "when": _parse(row["settled_at"]),
                "what": f'{row["pieces"]:,} pieces of {row["item_name"]} sold at auction',
                "detail": f'{money_text(row["winning_amount"])} to {_short_id(row["winner"])}',
                "tone": "s-done",
            })

        for row in c.execute(
            "SELECT id, name, location, winner, winning_amount, settled_at "
            "  FROM land_listings "
            " WHERE status = 'settled' AND winner IS NOT NULL "
            " ORDER BY settled_at DESC LIMIT ?", (limit,),
        ).fetchall():
            where = f' ({row["location"]})' if row["location"] else ""
            events.append({
                "kind": "plot",
                "when": _parse(row["settled_at"]),
                "what": f'{row["name"]}{where} sold',
                "detail": f'{money_text(row["winning_amount"])} to {_short_id(row["winner"])}',
                "tone": "s-done",
            })

    # None sorts last: an event with an unreadable stamp is still an event.
    events.sort(key=lambda e: (e["when"] is not None, e["when"]), reverse=True)
    return events[:limit]


async def history(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)

    try:
        events = recent_events()
        read_failed = False
    except Exception:  # noqa: BLE001 -- the page still renders without the list
        events, read_failed = [], True

    if read_failed:
        listing = ('<p class="notice">The activity list could not be read just now. '
                   'Nothing has changed &mdash; try again in a moment.</p>')
    elif events:
        rows = "".join(
            f'<tr><td>{esc(e["what"])}</td>'
            f'<td class="{e["tone"]}">{esc(e["detail"])}</td>'
            f'<td class="dim">{esc(_when_text(e["when"]))}</td></tr>'
            for e in events
        )
        listing = (f'<div class="tablewrap"><table><thead><tr>'
                   f'<th>What happened</th><th>Settled</th><th>When</th>'
                   f'</tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        listing = '<p class="empty">Nothing has been completed yet.</p>'

    body = f"""
<div class="hero">
<h1>History</h1>
<p>Work paid for, lots won and plots sold &middot; the last {ROW_LIMIT} things to finish
at New Orleans.</p>
</div>
{listing}
"""
    return page("History", "history", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/history", history)
