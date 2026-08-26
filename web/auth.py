"""Discord OAuth2 login, sessions, and the staff allowlist.

OAuth2 only -- there is no code-mint `/website_login` path; that shortcut
only makes sense bundled with a running bot process, and this site must
answer even when the bot is down. A browser session exists only because
someone completed a real Discord handshake.

Sessions live in `web_sessions` (one cookie, one store). Staff is a Discord
ID allowlist read from the environment, checked at the route -- never
inferred from anything the client sends.

Test seam: if `request.app` carries an `identity_provider` callable, that
callable is awaited instead of the cookie/session lookup. Tests install a
fake provider so login is bypassable without touching cookies, Discord, or
the database.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiohttp import ClientSession, web

from core.config import env_ids, env_str
from core.db import db_in

COOKIE_NAME = "nola_session"
SESSION_DAYS = 30

DISCORD_API = "https://discord.com/api"
AUTHORIZE_URL = f"{DISCORD_API}/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
ME_URL = f"{DISCORD_API}/users/@me"


def _client_id() -> Optional[str]:
    return env_str("NOLA_DISCORD_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return env_str("NOLA_DISCORD_CLIENT_SECRET")


def _redirect_uri() -> Optional[str]:
    return env_str("NOLA_DISCORD_REDIRECT_URI")


def _staff_ids() -> frozenset[str]:
    return frozenset(str(i) for i in env_ids("NOLA_STAFF_DISCORD_IDS"))


def is_staff(discord_id: str) -> bool:
    return str(discord_id) in _staff_ids()


@dataclass(frozen=True)
class Identity:
    subject: str          # wallet subject, e.g. "u:123456789"
    discord_id: str
    name: str
    staff: bool


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ sessions

def create_session(discord_id: str, name: str, *,
                    conn: Optional[sqlite3.Connection] = None) -> tuple[str, str]:
    """Open a session row for a Discord user. Returns (token, csrf)."""
    subject = f"u:{discord_id}"
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(16)
    expires = (datetime.now(timezone.utc)
               + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_in(conn) as c:
        c.execute(
            "INSERT INTO web_sessions (token, subject, name, csrf, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, subject, name, csrf, expires),
        )
    return token, csrf


def _session_row(token: str, *, conn: Optional[sqlite3.Connection] = None):
    with db_in(conn) as c:
        return c.execute(
            "SELECT * FROM web_sessions WHERE token = ? AND expires_at > ?",
            (token, _now()),
        ).fetchone()


def destroy_session(token: str, *, conn: Optional[sqlite3.Connection] = None) -> None:
    with db_in(conn) as c:
        c.execute("DELETE FROM web_sessions WHERE token = ?", (token,))


# ------------------------------------------------------------------ identity

async def resolve_identity(request: web.Request) -> Optional[Identity]:
    """The one function every page resolves its caller through.

    A page never reads the cookie or queries `web_sessions` directly -- it
    calls this, and only this.
    """
    provider = request.app.get("identity_provider")
    if provider is not None:
        return await provider(request)

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = _session_row(token)
    if row is None:
        return None
    subject = row["subject"]
    discord_id = subject.split(":", 1)[1] if subject.startswith("u:") else subject
    return Identity(
        subject=subject,
        discord_id=discord_id,
        name=row["name"] or discord_id,
        staff=is_staff(discord_id),
    )


# ------------------------------------------------------------------ routes

async def login(request: web.Request) -> web.Response:
    client_id, redirect_uri = _client_id(), _redirect_uri()
    if not client_id or not redirect_uri:
        return web.Response(text="Sign-in is not configured yet.", status=503)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return web.HTTPFound(f"{AUTHORIZE_URL}?{query}")


async def callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    if not code:
        return web.Response(text="Missing authorization code.", status=400)

    data = {
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
    }
    async with ClientSession() as http:
        async with http.post(TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                return web.Response(text="Sign-in with Discord failed.", status=502)
            token_body = await resp.json()
        access_token = token_body.get("access_token")
        async with http.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"}) as resp:
            if resp.status != 200:
                return web.Response(text="Could not read the Discord profile.", status=502)
            profile = await resp.json()

    discord_id = str(profile["id"])
    name = profile.get("username", discord_id)
    token, _csrf = create_session(discord_id, name)

    resp = web.HTTPFound("/me")
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400,
                     httponly=True, samesite="Lax")
    return resp


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        destroy_session(token)
    resp = web.HTTPFound("/")
    resp.del_cookie(COOKIE_NAME)
    return resp


def register(app: web.Application) -> None:
    app.router.add_get("/login", login)
    app.router.add_get("/auth/callback", callback)
    app.router.add_get("/logout", logout)
