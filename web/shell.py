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

from .theme import CSS
from .auth import Identity

NAV: list[tuple[str, str, str]] = [
    ("Storefront", "/", "storefront"),
    ("Stock", "/stock", "stock"),
    ("Account", "/me", "account"),
]
STAFF_NAV: tuple[str, str, str] = ("Ledger", "/ledger", "ledger")


def esc(value: object) -> str:
    """Escape text for safe placement in the page. The one escaping
    function every page module uses -- nothing here builds HTML by hand."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _nav_html(nav_key: str, identity: Optional[Identity]) -> str:
    items = list(NAV)
    if identity is not None and identity.staff:
        items.append(STAFF_NAV)
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
<header class="masthead">
  <a class="brand" href="/"><span class="wordmark">New Orleans</span></a>
  <nav class="nav">{_nav_html(nav_key, identity)}</nav>
  <div class="who-wrap">{_who_html(identity)}</div>
</header>
<main>
{body}
</main>
<footer class="foot">New Orleans market — daily sheet.</footer>
</body>
</html>"""
    return web.Response(text=doc, content_type="text/html", charset="utf-8", status=status)
