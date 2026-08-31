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


# Host-injected first, ours second. A panel allocates the port and routes
# traffic to it; our own preference is only meaningful when nothing else
# decided. Pterodactyl (Wispbyte) injects SERVER_PORT, most PaaS hosts
# inject PORT.
#
# Getting this wrong is invisible: binding the wrong port SUCCEEDS. The
# process starts, prints nothing unusual, and every request from the panel
# arrives at a port with nothing on it. There is no shell to check with, so
# the bound address is printed at startup -- it is the only evidence there is.
PORT_SOURCES = ("SERVER_PORT", "PORT", "NOLA_WEB_PORT")
DEFAULT_PORT = 8080


def resolve_port() -> tuple[int, str]:
    """Return (port, which variable supplied it). The name is for the log
    line: "8080 (default)" and "8080 (NOLA_WEB_PORT)" are very different
    situations when a site is unreachable."""
    for name in PORT_SOURCES:
        value = env_int(name)
        if value is not None:
            return value, name
    return DEFAULT_PORT, "default"


def main() -> None:
    init_db()
    app = create_app()
    port, source = resolve_port()
    # 0.0.0.0 rather than SERVER_IP: inside a container that variable can be
    # the allocation's public address, which is not on any local interface,
    # and binding it fails outright. Every allocation reaches us on all
    # interfaces anyway.
    print(f"web: binding 0.0.0.0:{port} (from {source})", flush=True)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
