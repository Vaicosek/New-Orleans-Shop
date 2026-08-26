"""Web tests: real aiohttp TestClient/TestServer against a temp SQLite DB.

Same style as test_money.py/test_shop.py -- plain script, real database, no
mocks. Login is bypassed through the `identity_provider` seam `web.auth`
exposes for exactly this purpose, so these tests exercise the actual routes,
the actual shell, and the actual staff gate without a live Discord flow.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-web-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_STAFF_DISCORD_IDS"] = ""
# Configured so /login and /auth/callback run their real logic (state cookie,
# state-mismatch guard) instead of the "not configured yet" 503 short-circuit.
# No real network call is ever made in this file -- every callback test below
# is rejected by the state check before the Discord HTTP call happens.
os.environ["NOLA_DISCORD_CLIENT_ID"] = "test-client-id"
os.environ["NOLA_DISCORD_CLIENT_SECRET"] = "test-client-secret"
os.environ["NOLA_DISCORD_REDIRECT_URI"] = "http://testserver/auth/callback"

from core import db, catalog                                     # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def reset() -> None:
    with db.db() as c:
        for t in ("order_claims", "orders", "stock_alerts", "stock", "items",
                  "ledger_entries", "ledger_holds", "wallet_flags", "idempotency",
                  "web_sessions", "wallets"):
            c.execute(f"DELETE FROM {t}")


db.init_db()


async def main() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from web import auth
    from web.auth import Identity
    from web.server import create_app

    reset()

    # -- create_session: the REAL path, no identity_provider seam ----------
    # This is the exact call `web.auth.callback()` makes after a real Discord
    # handshake. It used to throw: `web_sessions.subject` is a foreign key
    # into `wallets(subject)`, and a Discord user signing in for the first
    # time has no wallet row yet, so every real sign-in 500'd. Nothing above
    # this point exercises that path -- the identity_provider seam installed
    # below bypasses it entirely -- so this runs before that seam exists.
    real_token, real_csrf = auth.create_session("777", "Real Session User")
    with db.db() as c:
        wallet_row = c.execute(
            "SELECT 1 FROM wallets WHERE subject = ?", ("u:777",)
        ).fetchone()
    check("create_session does not raise for a Discord id with no prior wallet",
          True)
    check("create_session ensures the wallet row its FK requires",
          wallet_row is not None)

    real_app = create_app()
    async with TestClient(TestServer(real_app)) as real_client:
        r = await real_client.get("/me", cookies={auth.COOKIE_NAME: real_token})
        check("a real session cookie resolves through resolve_identity's actual "
              "cookie/session-row branch and /me answers 200", r.status == 200)
        text = await r.text()
        check("the real session's name renders on the page",
              "Real Session User" in text)

        # -- logout: the session's own csrf token is actually checked -------
        r = await real_client.get(f"/logout?csrf={real_csrf}",
                                   cookies={auth.COOKIE_NAME: real_token},
                                   allow_redirects=False)
        check("logout with the correct csrf token is accepted",
              r.status in (302, 303))
        with db.db() as c:
            row = c.execute(
                "SELECT 1 FROM web_sessions WHERE token = ?", (real_token,)
            ).fetchone()
        check("a correct-csrf logout actually destroys the session row",
              row is None)

        tok2, csrf2 = auth.create_session("778", "Second Real User")
        r = await real_client.get("/logout?csrf=not-the-real-token",
                                   cookies={auth.COOKIE_NAME: tok2},
                                   allow_redirects=False)
        check("logout with the wrong csrf token is refused", r.status == 403)
        with db.db() as c:
            row = c.execute(
                "SELECT 1 FROM web_sessions WHERE token = ?", (tok2,)
            ).fetchone()
        check("a refused logout leaves the session intact", row is not None)

    # -- OAuth2 state: minted at /login, required at /auth/callback ---------
    async with TestClient(TestServer(create_app())) as oauth_client:
        r = await oauth_client.get("/login", allow_redirects=False)
        check("/login redirects to Discord's authorize endpoint",
              r.status in (302, 303)
              and "discord.com" in r.headers.get("Location", ""))
        check("/login's redirect carries a state parameter",
              "state=" in r.headers.get("Location", ""))
        login_state = r.cookies.get(auth.STATE_COOKIE_NAME)
        check("/login sets a state cookie", login_state is not None)

        r = await oauth_client.get("/auth/callback?code=abc")
        check("/auth/callback rejects a callback with no state at all",
              r.status == 400)

        r = await oauth_client.get(
            "/auth/callback?code=abc&state=not-the-real-state",
            cookies={auth.STATE_COOKIE_NAME: "the-real-state"},
        )
        check("/auth/callback rejects a state that doesn't match its cookie",
              r.status == 400)

    reset()
    app = create_app()
    identity_box: dict = {"value": None}

    async def fake_identity_provider(request):
        return identity_box["value"]

    app["identity_provider"] = fake_identity_provider

    async with TestClient(TestServer(app)) as client:
        # -- public routes answer with no session, domain layer empty ------
        r = await client.get("/")
        check("storefront answers 200 with no session and an empty catalog",
              r.status == 200)
        text = await r.text()
        check("empty storefront is one plain line, not placeholder rows",
              "Nothing stocked yet." in text)

        r = await client.get("/stock")
        check("stock answers 200 with no session and an empty catalog", r.status == 200)

        r = await client.get("/health")
        check("/health answers 200 (must work even if the bot is down)", r.status == 200)

        # -- customer and staff pages refuse anonymous visitors ------------
        r = await client.get("/me")
        check("/me 401s anonymous", r.status == 401)

        r = await client.get("/ledger")
        check("/ledger 401s anonymous", r.status == 401)

        # -- a price row states both bases ----------------------------------
        catalog.add_item("Honeycomb Block", 300, stack_size=64)
        r = await client.get("/stock")
        text = await r.text()
        check("stock page lists the seeded item", "Honeycomb Block" in text)
        check("price row states the stack basis", "stack of 64" in text)
        check("price row states the per-piece basis", "/ piece" in text)

        # -- a signed-in non-staff visitor: 403 on /ledger, no staff nav ----
        identity_box["value"] = Identity(subject="u:1", discord_id="1",
                                          name="Rank and File", staff=False)
        r = await client.get("/ledger")
        check("/ledger 403s a logged-in non-staff user", r.status == 403)

        r = await client.get("/me")
        check("/me answers 200 for a signed-in customer", r.status == 200)
        text = await r.text()
        check("staff nav entry is absent from a non-staff render, not just CSS-hidden",
              'href="/ledger"' not in text)

        # -- staff: /ledger answers, nav carries the entry ------------------
        identity_box["value"] = Identity(subject="u:2", discord_id="2",
                                          name="Owner", staff=True)
        r = await client.get("/ledger")
        check("/ledger answers 200 for staff", r.status == 200)
        text = await r.text()
        check("staff nav entry is present for a staff render", 'href="/ledger"' in text)


asyncio.run(main())

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all web tests pass")
