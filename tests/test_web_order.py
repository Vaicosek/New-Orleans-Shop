"""Storefront grid + `/order`: the website's first state-changing route.

Same style as test_web.py -- real aiohttp TestClient against a temp SQLite
DB, login bypassed through the `identity_provider` seam. This file exists
to pin the things CONTRACT.md section 12's new paragraph promises:

  [1] the grid renders an icon (a real mapped item gets an <img>, an
      unmapped one gets a monogram tile, never a broken image request)
  [2] an anonymous visitor gets a "sign in" link, never a live-looking
      form it cannot submit
  [3] a signed-in customer's form actually opens a real order --
      `core.orders.create_order`, same as Discord's shop panel -- with the
      right price snapshot and creator subject
  [4] every one of the four ways this route can be attacked (no csrf, wrong
      csrf, anonymous, tampered item id, non-positive pieces) is refused
      and leaves no order behind
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-web-order-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_STAFF_DISCORD_IDS"] = ""

from core import catalog, db                                     # noqa: E402

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


def order_count() -> int:
    with db.db() as c:
        return c.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]


async def main() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from web.auth import Identity
    from web.server import create_app

    reset()
    oak_id = catalog.add_item("Oak Log", 1, price_unit_pieces=64, stack_size=64)
    catalog.add_item("Unmapped Mystery Item", 5, price_unit_pieces=1, stack_size=1)

    app = create_app()
    identity_box: dict = {"value": None}

    async def fake_identity_provider(request):
        return identity_box["value"]

    app["identity_provider"] = fake_identity_provider

    async with TestClient(TestServer(app)) as client:
        # -- [1] icon rendering -------------------------------------------
        r = await client.get("/")
        text = await r.text()
        check("storefront renders the item grid container", 'class="itemgrid"' in text)
        check("a mapped item (Oak Log) renders a real <img> icon",
              '<img class="icon"' in text)
        check("an unmapped item renders a monogram fallback tile, not a broken image",
              'class="icon icon-fallback"' in text and ">U<" in text)

        # -- [2] anonymous visitor: link, never a live-looking form --------
        check("anonymous visitor sees a sign-in link, not an order form",
              'class="order-link"' in text and 'class="order-form"' not in text)

        r = await client.post("/order", data={"item_id": str(oak_id), "pieces": "64"})
        check("POST /order 401s an anonymous visitor", r.status == 401)
        check("an anonymous POST opens no order", order_count() == 0)

        # -- [3] signed-in customer: real form, real order ------------------
        # A real session always carries a real random csrf (auth.create_session
        # mints one) -- this fake Identity sets one explicitly so the test
        # matches that reality instead of accidentally testing an empty-vs-
        # empty csrf match that can never happen against a real session.
        identity_box["value"] = Identity(subject="u:1", discord_id="1",
                                          name="Regular Customer", staff=False,
                                          csrf="test-csrf-token-abc123")
        r = await client.get("/")
        text = await r.text()
        check("signed-in visitor sees a real order form", 'class="order-form"' in text)
        check("the form carries the item id", f'value="{oak_id}"' in text)

        # Pull the real csrf token straight off the rendered form -- the
        # same thing a real browser submitting the real page would do,
        # rather than reaching into web_sessions directly.
        import re
        m = re.search(r'name="csrf" value="([^"]+)"', text)
        check("the rendered form carries a csrf token", m is not None)
        real_csrf = m.group(1) if m else ""

        r = await client.post("/order", data={"item_id": str(oak_id), "pieces": "64",
                                                "csrf": real_csrf},
                               allow_redirects=False)
        check("a correct-csrf order POST redirects back to the storefront",
              r.status in (302, 303))
        check("the redirect carries the new order id",
              "ordered=" in r.headers.get("Location", ""))
        check("exactly one order now exists", order_count() == 1)
        with db.db() as c:
            row = c.execute(
                "SELECT item_id, requested_pieces, created_by, price_coins, price_unit_pieces "
                "FROM orders ORDER BY id DESC LIMIT 1"
            ).fetchone()
        check("the order is for the right item", row["item_id"] == oak_id)
        check("the order requests the submitted quantity", row["requested_pieces"] == 64)
        check("the order is attributed to the signed-in customer, not a guessed subject",
              row["created_by"] == "u:1")
        check("the order snapshots the item's current price basis",
              row["price_coins"] == 1 and row["price_unit_pieces"] == 64)

        with db.db() as c:
            new_id = c.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()["id"]
        r = await client.get(f"/?ordered={new_id}")
        text = await r.text()
        check("the storefront shows a plain-text confirmation notice, no banner box",
              f"Order #{new_id} opened" in text and 'class="notice"' in text)

        # -- [4] every attack the route can see is refused, no order opens --
        before = order_count()

        r = await client.post("/order", data={"item_id": str(oak_id), "pieces": "10"})
        check("a POST with no csrf field at all is refused", r.status == 403)

        r = await client.post("/order", data={"item_id": str(oak_id), "pieces": "10",
                                                "csrf": "not-the-real-token"})
        check("a POST with the wrong csrf is refused", r.status == 403)

        r = await client.post("/order", data={"item_id": "999999", "pieces": "10",
                                                "csrf": real_csrf})
        check("a POST for a nonexistent item id is refused", r.status == 400)

        r = await client.post("/order", data={"item_id": str(oak_id), "pieces": "0",
                                                "csrf": real_csrf})
        check("a POST requesting zero pieces is refused", r.status == 400)

        r = await client.post("/order", data={"item_id": str(oak_id), "pieces": "-5",
                                                "csrf": real_csrf})
        check("a POST requesting negative pieces is refused", r.status == 400)

        check("none of the refused attempts opened an order",
              order_count() == before, f"before={before} after={order_count()}")


asyncio.run(main())

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all storefront-grid / order-route tests pass")
