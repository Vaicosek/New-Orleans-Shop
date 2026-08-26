"""Web entrypoint for New Orleans.

Applies the schema (idempotent) and serves the site. Supervised by
`run_all.py` in production; runnable directly for local development.
"""
from __future__ import annotations


from aiohttp import web

from core.config import env_int
from core.db import init_db
from web.server import create_app


def main() -> None:
    init_db()
    app = create_app()
    port = env_int("PORT", default=env_int("NOLA_WEB_PORT", default=8080))
    web.run_app(app, port=port)


if __name__ == "__main__":
    main()
