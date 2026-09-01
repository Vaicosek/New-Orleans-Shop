"""`/help` and `/terms` -- public, and the only pages here that touch no
database at all.

That is deliberate: they answer when the database is locked, mid-migration
or gone, which is exactly when somebody goes looking for "how does this
work" or "who do I ask". Every fact below is transcribed from CONTRACT.md
by hand rather than generated, so nothing here can drift with a schema
change without a person noticing.

The section 9 wall applies to this file more than to any other page under
`web/`: a page explaining the shop is the likeliest place for a mention of
what the site must never let on exists. This file covers commerce only --
ordering, ranks, auctions, land, bonds, loans and teams -- and no other
part of the network.
"""
from __future__ import annotations

from aiohttp import web

from ..auth import resolve_identity
from ..shell import page

HELP_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "What this is",
        "New Orleans is a player-run shop. Members request the goods they want, other "
        "members produce and deliver them, and the shop pays for the work out of its own "
        "treasury. Everything on this website is a window on that: the storefront and "
        "inventory are what is stocked, the work board is what is being made right now, "
        "and history is what has already been finished.",
    ),
    (
        "The currency",
        "Everything is priced in gold ingots, written with the symbol g after the number "
        "&mdash; 1,450 g. Amounts are always whole numbers; there is no smaller unit and "
        "nothing is ever rounded behind your back. A price is quoted two ways at once, "
        "per stack and per piece, so you never have to do the division yourself.",
    ),
    (
        "Ordering something",
        "Anyone signed in can open a request from the storefront: tick the goods you want, "
        "say how many pieces of each, and submit. That opens one order per item, and the "
        "order records the price at the moment it was opened &mdash; if the shop reprices "
        "that good tomorrow, work already in flight is unaffected.",
    ),
    (
        "Doing the work",
        "Open orders appear on the work board here and on the /orders command in Discord. "
        "A member claims one, produces the goods, hands them over on the server, and marks it "
        "delivered. Staff then check the delivery and approve it, and approving is what "
        "actually pays &mdash; the gold moves from the shop treasury to the person who did "
        "the work, in one step, never in advance.",
    ),
    (
        "Ranks",
        "Members hold a rank &mdash; Recruit, Worker, Veteran, Expert, then Elite. It is "
        "earned by what you have actually been paid for completed work and by what you "
        "have spent winning lots, and it is recomputed from those records every time it is "
        "read, never stored as a running total that could drift. A higher rank pays a "
        "bonus on top of every order you complete, and raises how much the shop will lend "
        "you. Your own rank and bonus are on your hub page.",
    ),
    (
        "Auctions and land",
        "The shop sells item lots and land plots by open auction. The highest bid when the "
        "clock runs out takes it, and the money is held in escrow from the moment you bid "
        "&mdash; the instant somebody outbids you, your hold is released in full, so you "
        "are never quietly short. Some plots carry a buy-now price, and a bid that reaches "
        "it ends the sale on the spot. You can watch both on this site, but bidding itself "
        "happens on the listing's card in Discord.",
    ),
    (
        "Bonds and loans",
        "The treasury issues bonds: you buy units at a fixed price, collect a coupon at a "
        "fixed interval, and get your money back when the bond matures. Loans work the "
        "other way &mdash; you borrow from the treasury up to a limit set by your rank, at "
        "a flat rate of interest fixed when the loan is issued, and repay within its term. "
        "Both live on the /wallet command in Discord.",
    ),
    (
        "Teams",
        "A manager can run a team and members can join one. It is a roster and a name: "
        "being on a team changes nothing about what you are paid or what you may do. Every "
        "team is listed on this site; joining and leaving happen on the /team command in "
        "Discord.",
    ),
    (
        "Getting help",
        "Ask staff in the Discord server. Handing goods over, approving deliveries and "
        "listing plots are all done by people, so a person is always the fastest answer.",
    ),
)

TERMS_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "Gold is not money",
        "Gold ingots exist inside a Minecraft server and have no real-world value. Nothing "
        "here can be bought with, or exchanged for, real currency, and no balance shown on "
        "this site is a debt owed to you in anything but gold.",
    ),
    (
        "Delivery is done by people",
        "The shop does not move items automatically. Goods you order, lots you win and "
        "plots you buy are handed over on the server by a member of staff. Balances and orders "
        "are recorded the moment they change; the handover happens when somebody is "
        "online to do it.",
    ),
    (
        "When you get paid",
        "Completed work is paid when staff approve the delivery, out of the shop treasury. "
        "If the treasury cannot cover a payment at that moment, the payment waits &mdash; "
        "it is never partially made, and it is not forgotten.",
    ),
    (
        "Prices and listings change",
        "Prices, stock, thresholds and what is listed can change at any time. A price "
        "recorded on an order that is already open does not change with them.",
    ),
    (
        "Accounts",
        "Signing in uses your Discord account and nothing else; the shop stores your "
        "Discord id, not a password. Staff can block an account from ordering or from "
        "holding a balance where there is abuse &mdash; charging for work not done, "
        "reversing a handover, or using another member's account.",
    ),
    (
        "Records",
        "Every movement of gold is written to a ledger that is never edited or deleted. "
        "Your own history is on your hub page, and staff can read the full ledger. If you "
        "think a figure is wrong, ask &mdash; the record of what happened exists.",
    ),
)


def _sections_html(sections: tuple[tuple[str, str], ...]) -> str:
    return "".join(f"<h2>{title}</h2>\n<p>{text}</p>\n" for title, text in sections)


async def help_page(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    body = f"""
<div class="hero">
<h1>How the shop works</h1>
<p>What New Orleans is, how an order becomes gold in your pocket, and where each thing
is done.</p>
</div>
{_sections_html(HELP_SECTIONS)}
"""
    return page("Help", "help", body, identity=identity)


async def terms(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)
    body = f"""
<div class="hero">
<h1>Terms</h1>
<p>The short, plain version. There is no long version.</p>
</div>
{_sections_html(TERMS_SECTIONS)}
"""
    return page("Terms", "terms", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/help", help_page)
    app.router.add_get("/terms", terms)
