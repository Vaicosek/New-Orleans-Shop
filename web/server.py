"""App assembly for the New Orleans website.

Each domain module registers its own routes in isolation: if one fails to
import or to register, that failure 404s only its own routes, with a
message naming what broke, instead of taking the whole process down.
`/health` is registered directly and answers regardless of anything below
it -- it must work even when every page module is broken.
"""
from __future__ import annotations

import importlib
import logging

from aiohttp import web

logger = logging.getLogger("nola.web")

# (module, the routes it owns) -- listed explicitly so a module that fails to
# import still gets an honest, named 404 on its own paths rather than
# aiohttp's generic "no route" or a dead process.
PAGE_MODULES: list[tuple[str, list[str]]] = [
    ("web.auth", ["/login", "/auth/callback", "/logout"]),
    ("web.pages.storefront", ["/", "/stock"]),
    ("web.pages.account", ["/me"]),
    ("web.pages.ledger", ["/ledger"]),
]


def _broken_handler(name: str):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=f"{name} is unavailable.", status=404)
    return handler


def register_all(app: web.Application) -> None:
    for name, routes in PAGE_MODULES:
        try:
            module = importlib.import_module(name)
            module.register(app)
        except Exception:  # noqa: BLE001 -- isolation between modules is the point
            logger.exception("web module %s failed to register; its routes will 404", name)
            handler = _broken_handler(name)
            for route in routes:
                app.router.add_get(route, handler)


async def health(request: web.Request) -> web.Response:
    """Public, dependency-free. Answers even if the bot process is down and
    even if every other web module failed to register."""
    return web.Response(text="ok", content_type="text/plain")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    register_all(app)
    return app
