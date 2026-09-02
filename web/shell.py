"""The one shared page chrome. Every route in web/ renders through `page()`.

Plain f-string templating -- no Jinja, no template directory -- same
convention as the codebase this project's patterns are lifted from. Nav is a
fixed list of (label, href, key) tuples; the staff entry is appended only
when the caller passes a staff identity, so a non-staff render never
contains that entry at all. It is omitted server-side, not hidden by CSS.
"""
from __future__ import annotations

from typing import Optional

from aiohttp import web

from core.money import balance
from core.pricing import money_text

from .theme import CSS
from .auth import Identity

# The city's mark, drawn once and reused. New Orleans is French-founded and
# signs itself with the fleur-de-lis on the flag, the manhole covers and the
# corner tiles; it is the one figure on this site that is not a number. It
# now also doubles as the monogram-canvas repeat in theme.py's background
# patterns, the same way a maker's own emblem becomes its print.
# Inline so the page carries no image request, and `currentColor` so it
# takes the gold from the masthead and the muted grey from the footer
# without a second copy.
FLEUR = (
    '<svg class="lis" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">'
    '<path d="M12 1.4c-1.6 2.1-2.5 3.9-2.5 5.5 0 1.4.6 2.6 1.5 3.6H9.2'
    'c-2.6 0-4.6 1.5-4.6 3.7 0 1.8 1.3 3.1 3 3.1 1.3 0 2.3-.8 2.3-1.8'
    ' 0-.8-.6-1.4-1.3-1.4-.4 0-.8.1-1 .4.2-.9.9-1.4 2-1.4h1.6v3.2'
    'c0 2.1-.6 3.5-1.9 5.1h5.4c-1.3-1.6-1.9-3-1.9-5.1v-3.2h1.6'
    'c1.1 0 1.8.5 2 1.4-.2-.3-.6-.4-1-.4-.7 0-1.3.6-1.3 1.4 0 1 1 1.8 2.3 1.8'
    ' 1.7 0 3-1.3 3-3.1 0-2.2-2-3.7-4.6-3.7H13c.9-1 1.5-2.2 1.5-3.6'
    ' 0-1.6-.9-3.4-2.5-5.5z"/>'
    '<path d="M8.5 11.6h7v1.3h-7z"/>'
    '</svg>'
)


NAV: list[tuple[str, str, str]] = [
    ("Storefront", "/", "storefront"),
    ("Inventory", "/inventory", "stock"),
    ("Auctions", "/auctions", "auctions"),
    ("Land", "/land", "land"),
    ("Orders", "/orders", "orders"),
    ("Teams", "/teams", "teams"),
    ("History", "/history", "history"),
    ("Hub", "/me", "account"),
    ("Help", "/help", "help"),
]
#: Staff-only nav entries, appended for a staff identity and rendered by
#: nobody else -- omitted server-side, never CSS-hidden.
STAFF_NAV: tuple[str, str, str] = ("Ledger", "/ledger", "ledger")
STAFF_NAV_EXTRA: tuple[tuple[str, str, str], ...] = (
    ("Solvency", "/solvency", "solvency"),
)


def esc(value: object) -> str:
    """Escape text for safe placement in the page. The one escaping
    function every page module uses -- nothing here builds HTML by hand."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _nav_html(nav_key: str, identity: Optional[Identity]) -> str:
    items = list(NAV)
    if identity is not None and identity.staff:
        items.append(STAFF_NAV)
        items.extend(STAFF_NAV_EXTRA)
    parts = []
    for label, href, key in items:
        current = ' aria-current="page"' if key == nav_key else ""
        parts.append(f'<a class="navlink" href="{href}"{current}>{label}</a>')
    return "".join(parts)


def _who_html(identity: Optional[Identity]) -> str:
    if identity is None:
        return '<a class="navlink" href="/login">Sign in with Discord</a>'
    logout_href = f"/logout?csrf={esc(identity.csrf)}"
    return (f'<span class="who">{esc(identity.name)}</span>'
            f'<a class="navlink" href="{logout_href}">Sign out</a>')


def _wallet_html(identity: Optional[Identity]) -> str:
    """The signed-in visitor's money, above everything else.

    Anonymous visitors get nothing here -- an empty strip reading "0 g" would
    be decorating an absence, and it would also be a lie. A balance lookup
    that fails renders nothing rather than a zero: "we could not read it" and
    "you have none" are different facts and must never look the same.
    """
    if identity is None:
        return ""
    try:
        bal = balance(identity.subject)
    except Exception:  # noqa: BLE001 -- the page still renders without it
        return ""
    return (f'<div class="wallet">'
            f'<span>Wallet available <b>{money_text(bal.available)}</b></span>'
            f'<span>Held <b>{money_text(bal.held)}</b></span>'
            f'</div>')


def page(title: str, nav_key: str, body: str, *,
         identity: Optional[Identity] = None, status: int = 200) -> web.Response:
    """Render `body` inside the shared chrome. Returns a `web.Response`.

    `identity`, when given, decides whether the staff nav entry appears and
    what the header's sign-in area shows. A page passes whatever it already
    resolved through `auth.resolve_identity()` -- this function never
    resolves one itself.
    """
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — New Orleans</title>
<style>{CSS}</style>
</head>
<body>
{_wallet_html(identity)}
<header class="masthead">
  <a class="brand" href="/">{FLEUR}<span class="wordmark">New Orleans</span></a>
  <nav class="nav">{_nav_html(nav_key, identity)}</nav>
  <div class="who-wrap">{_who_html(identity)}</div>
</header>
<main>
{body}
</main>
<footer class="foot">{FLEUR}<span>New Orleans market — daily sheet.</span></footer>
</body>
</html>"""
    return web.Response(text=doc, content_type="text/html", charset="utf-8", status=status)
