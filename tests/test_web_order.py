"""Storefront grid + `/order`: the website's first state-changing route.

Same style as test_web.py -- real aiohttp TestClient against a temp SQLite
DB, login bypassed through the `identity_provider` seam. This file exists
to pin the things CONTRACT.md section 12's new paragraph promises for the
batch/cart form:

  [1] the grid renders an icon (a real mapped item gets an <img>, an
      unmapped one gets a monogram tile, never a broken image request)
  [2] an anonymous visitor gets a "sign in" link, never a live-looking
      form it cannot submit
  [3] a signed-in customer can check off several items at once, each with
      its own typed quantity field, and one POST opens a
      real order for every checked item -- `core.orders.create_order`,
      same as Discord's shop panel -- with the right price snapshot and
      creator subject
  [4] one bad line in the batch (a stale item id, a non-positive custom
      amount) never blocks the rest -- the redirect reports exactly how
      many opened and how many were skipped
  [5] every one of the ways this route can be attacked (no csrf, wrong
      csrf, anonymous, no items checked at all) is refused and leaves no
      order behind
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
    birch_id = catalog.add_item("Birch Log", 1, price_unit_pieces=64, stack_size=64)
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
        check("anonymous visitor sees a sign-in link, never cart controls",
              'class="order-link"' in text and 'class="cart-controls"' not in text)

        r = await client.post("/order", data={"items": str(oak_id), f"qty_{oak_id}": "64"})
        check("POST /order 401s an anonymous visitor", r.status == 401)
        check("an anonymous POST opens no order", order_count() == 0)

        # -- [3] signed-in customer: real form, real batch order ------------
        identity_box["value"] = Identity(subject="u:1", discord_id="1",
                                          name="Regular Customer", staff=False,
                                          csrf="test-csrf-token-abc123")
        r = await client.get("/")
        text = await r.text()
        check("signed-in visitor sees cart controls, not a sign-in link",
              'class="cart-controls"' in text and 'class="order-link"' not in text)
        check("each item carries its own checkbox",
              f'name="items" value="{oak_id}"' in text
              and f'name="items" value="{birch_id}"' in text)
        check("each item carries its own typed quantity field",
              f'name="qty_{oak_id}"' in text and f'name="qty_{birch_id}"' in text)
        check("the page carries a single cart-submit form with a real csrf token",
              'class="cart-submit"' in text and 'name="csrf" value="test-csrf-token-abc123"' in text)

        real_csrf = "test-csrf-token-abc123"

        # Two items in one batch: Oak Log at its default stack, Birch Log
        # with a typed custom amount.
        r = await client.post(
            "/order",
            data={
                "csrf": real_csrf,
                "items": [str(oak_id), str(birch_id)],
                f"qty_{oak_id}": "64",
                f"qty_{birch_id}": "200",
            },
            allow_redirects=False,
        )
        check("a correct-csrf batch POST redirects back to the storefront",
              r.status in (302, 303))
        location = r.headers.get("Location", "")
        check("the redirect carries both new order ids", "ordered=" in location
              and "," in location.split("ordered=")[1].split("&")[0])
        check("no failure count is reported when every item opened", "failed=" not in location)
        check("exactly two orders now exist", order_count() == 2)

        with db.db() as c:
            rows = c.execute(
                "SELECT item_id, requested_pieces, created_by, price_coins, price_unit_pieces "
                "FROM orders ORDER BY id ASC"
            ).fetchall()
        by_item = {row["item_id"]: row for row in rows}
        check("the Oak Log order requests the default stack quantity",
              by_item[oak_id]["requested_pieces"] == 64)
        check("the Birch Log order requests the typed quantity",
              by_item[birch_id]["requested_pieces"] == 200)
        check("both orders are attributed to the signed-in customer, not a guessed subject",
              all(row["created_by"] == "u:1" for row in rows))
        check("both orders snapshot their item's current price basis",
              all(row["price_coins"] == 1 and row["price_unit_pieces"] == 64 for row in rows))

        r = await client.get(f"/{location[location.index('?'):]}" if "?" in location else "/")
        text = await r.text()
        check("the storefront shows a plural, plain-text confirmation notice, no banner box",
              "Orders #" in text and 'class="notice"' in text)

        # -- [4] partial failure: one good item, one stale item id ----------
        reset()
        oak_id = catalog.add_item("Oak Log", 1, price_unit_pieces=64, stack_size=64)

        r = await client.post(
            "/order",
            data={
                "csrf": real_csrf,
                "items": [str(oak_id), "999999"],
                f"qty_{oak_id}": "64",
                "qty_999999": "64",
            },
            allow_redirects=False,
        )
        check("a batch with one good and one stale item id still redirects",
              r.status in (302, 303))
        location = r.headers.get("Location", "")
        check("the redirect reports exactly one opened order", location.count("ordered=") == 1
              and "," not in location.split("ordered=")[1].split("&")[0])
        check("the redirect reports exactly one failure", "failed=1" in location)
        check("exactly one order exists after the partial-failure batch", order_count() == 1)

        # A batch where every line is bad opens nothing and 400s.
        r = await client.post(
            "/order",
            data={"csrf": real_csrf, "items": "999999", "qty_999999": "64"},
        )
        check("a batch where every item fails is refused outright", r.status == 400)
        check("a fully-failed batch opens no order", order_count() == 1)

        # A checked item with a non-positive typed amount is skipped, not fatal.
        r = await client.post(
            "/order",
            data={
                "csrf": real_csrf,
                "items": str(oak_id),
                f"qty_{oak_id}": "0",
            },
        )
        check("a checked item with a zero quantity is refused (nothing else to open)",
              r.status == 400)
        check("a non-positive quantity opens no order", order_count() == 1)

        # -- [5] every attack the route can see is refused, no order opens --
        before = order_count()

        r = await client.post("/order", data={"items": str(oak_id), f"qty_{oak_id}": "64"})
        check("a POST with no csrf field at all is refused", r.status == 403)

        r = await client.post("/order", data={"items": str(oak_id), f"qty_{oak_id}": "64",
                                                "csrf": "not-the-real-token"})
        check("a POST with the wrong csrf is refused", r.status == 403)

        r = await client.post("/order", data={"csrf": real_csrf})
        check("a POST with no items checked at all is refused", r.status == 400)

        check("none of the refused attempts opened an order",
              order_count() == before, f"before={before} after={order_count()}")


asyncio.run(main())

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all storefront-grid / order-route (batch) tests pass")
