"""Web entrypoint for New Orleans.

Applies the schema (idempotent) and serves the site. Supervised by
`run_all.py` in production; runnable directly for local development.
"""
from __future__ import annotations


from aiohttp import web

# Before ANY core.* import: core.db resolves NOLA_DB_PATH at import time.
from core.env import load_env_file  # noqa: E402

load_env_file()

from core.config import env_int  # noqa: E402
from core.db import init_db  # noqa: E402
from web.server import create_app  # noqa: E402


def main() -> None:
    init_db()
    app = create_app()
    port = env_int("PORT", default=env_int("NOLA_WEB_PORT", default=8080))
    web.run_app(app, port=port)


if __name__ == "__main__":
    main()
