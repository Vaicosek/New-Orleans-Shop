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

    from web.auth import Identity
    from web.server import create_app

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
