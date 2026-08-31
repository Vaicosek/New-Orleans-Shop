"""Currency display: every money figure carries the `g` symbol, once, after
the number -- and the retired word "coin"/"coins" appears nowhere a user looks.

CONTRACT.md section 5: the currency is gold ingots, symbol `g` printed AFTER
the number, and `core.pricing.CURRENCY` is the ONE place that symbol is
defined. SLICE_CONTRACT.md section 9 pins the formatter (`pricing.money_text`)
and lists the four display defects this file guards:

  1. `/me` printed the balance as "1,000 coins" -- retired word, no symbol.
  2. `/me` claim payouts and history deltas were bare numbers.
  3. `/ledger` money cells were bare numbers.
  4. `bot/views/shop.py` hardcoded the literal `g` instead of the formatter.

Real routes, real aiohttp TestClient, real temp database -- the same shape as
tests/test_web.py. The bot check is a text scan, because `bot.views.shop`
imports `discord`; the scan asserts the symbol is not typed in that file at
all, which is the only way "imports CURRENCY rather than hardcoding" can be
stated about source text.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-currency-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_STAFF_DISCORD_IDS"] = ""

from core import audit, catalog, db, money, orders as orders_core, pricing  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def raises(name: str, exc: type, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc:
        check(name, True)
    except Exception as err:  # noqa: BLE001
        check(name, False, f"raised {type(err).__name__}: {err}")
    else:
        check(name, False, "did not raise")


def reset() -> None:
    with db.db() as c:
        for t in ("order_claims", "orders", "stock_alerts", "stock", "items",
                  "ledger_entries", "ledger_holds", "wallet_flags", "idempotency",
                  "audit_actions", "web_sessions", "wallets"):
            c.execute(f"DELETE FROM {t}")


db.init_db()

# The retired currency word, as a user would read it: "coin", "coins", "Coins".
# Column and key names (`price_coins`, `paid_coins`, `money_coins`) are internal
# identifiers and never reach the page, so a hit in rendered HTML is a real one.
RETIRED = re.compile(r"\bcoins?\b", re.IGNORECASE)


# ---------------------------------------------------------------- [1] formatter
print("[1] core.pricing.money_text -- the one formatter")

check("money_text exists in core.pricing (web/ cannot import bot.ui.embed)",
      hasattr(pricing, "money_text"))
if hasattr(pricing, "money_text"):
    mt = pricing.money_text
    check("money_text(300) == '300 g' -- symbol AFTER the number", mt(300) == "300 g",
          repr(mt(300)))
    check("money_text separates thousands", mt(1450) == "1,450 g", repr(mt(1450)))
    check("money_text(0) is not a bare number", mt(0) == "0 g", repr(mt(0)))
    check("money_text uses pricing.CURRENCY, not a literal",
          mt(7).endswith(f" {pricing.CURRENCY}"))
    check("sign=True prefixes '+' for a positive delta", mt(500, sign=True) == "+500 g",
          repr(mt(500, sign=True)))
    check("sign=True leaves a negative's own '-' alone",
          mt(-500, sign=True) == "-500 g", repr(mt(-500, sign=True)))
    check("sign=True does not sign zero", mt(0, sign=True) == "0 g",
          repr(mt(0, sign=True)))
    raises("money_text rejects a bool", TypeError, mt, True)
    raises("money_text rejects a float", TypeError, mt, 1.5)
    raises("money_text rejects a str", TypeError, mt, "300")

    # The bot delegate must stay byte-identical: every existing panel reads
    # through it and none of those files may be edited.
    sys.path.insert(0, str(ROOT / "tests" / "_stubs"))
    import discord_stub  # noqa: E402
    discord_stub.install()
    try:
        from bot.ui import embed as bot_embed  # noqa: E402
    except Exception as err:  # noqa: BLE001
        check("bot.ui.embed imports for the delegate check", False, repr(err))
    else:
        check("bot.ui.embed.money_text delegates -- byte-identical output",
              all(bot_embed.money_text(n) == pricing.money_text(n)
                  for n in (0, 1, 300, 1450, -75, 10 ** 9)))
        raises("the delegate still rejects a bool", TypeError, bot_embed.money_text, True)


# --------------------------------------------------------------- [2] bot/shop.py
print()
print("[2] bot/views/shop.py -- the quote confirmation imports the formatter")

shop_src = (ROOT / "bot" / "views" / "shop.py").read_text()
check("shop.py no longer hardcodes the currency symbol in display text",
      not re.search(r"""[,:][^"'\n]*\}\s*g\b""", shop_src),
      "found a literal ' g' next to a formatted number")
check("shop.py renders the order total through money_text",
      "money_text(quote['total_coins'])" in shop_src
      or 'money_text(quote["total_coins"])' in shop_src)
check("shop.py imports money_text from ..ui.embed",
      re.search(r"from \.\.ui\.embed import [^\n]*\bmoney_text\b", shop_src) is not None)
check("shop.py does not format the total as a bare number",
      "{quote['total_coins']:,}" not in shop_src
      and '{quote["total_coins"]:,}' not in shop_src)
# Docstrings and comments are read by maintainers, but the strings a Discord
# user sees must be free of the retired word. `*_coins` identifiers inside an
# f-string are internal names, not text, so they are stripped before the scan.
user_strings = re.findall(r'f?"([^"\n]{4,})"', shop_src)
offenders = [s for s in user_strings if RETIRED.search(re.sub(r"\w*_coins\w*", "", s))]
check("no retired currency word in a shop.py user-facing string",
      not offenders, str(offenders))


# ------------------------------------------------------------------- [3] the web
print()
print("[3] /me and /ledger -- no bare money figures, no retired word")


async def main() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from web.auth import Identity
    from web.server import create_app

    reset()

    subject = "u:4001"
    money.ensure_wallet(subject)
    money.mint(subject, 1_000, service="owner", reason="opening float")
    money.mint(subject, 500, service="owner", reason="order payout",
               ref_kind="order", ref_id="1")

    item_id = catalog.add_item("Honeycomb Block", 300, stack_size=64)
    order_id = orders_core.create_order(item_id, 64, created_by="u:1")
    orders_core.claim(order_id, subject, 64)
    orders_core.mark_fulfilled(order_id, subject, 64)
    with db.db() as c:
        # paid_coins and paid_event move together (schema CHECK); this test only
        # needs a paid claim to render, not the approve path another slice owns.
        c.execute("UPDATE order_claims SET paid_coins = 300, paid_event = ? "
                  " WHERE order_id = ? AND worker = ?",
                  (money.new_event_id("order.pay"), order_id, subject))
        audit.record(c, actor="u:1", target=subject, kind="treasury.fund",
                     summary="opening float", ops=[], money_coins=1_000)

    bal = money.balance(subject)
    app = create_app()
    identity_box: dict = {"value": None}

    async def fake_identity_provider(request):
        return identity_box["value"]

    app["identity_provider"] = fake_identity_provider

    async with TestClient(TestServer(app)) as client:
        identity_box["value"] = Identity(subject=subject, discord_id="4001",
                                         name="Rank and File", staff=False)
        r = await client.get("/me")
        check("/me answers 200 for a signed-in customer", r.status == 200)
        me_text = await r.text()

        mt = pricing.money_text
        # The balance moved from a prose line to a ruled definition list (the
        # accepted hub shape). The assertion follows the INTENT, not the old
        # markup: all three figures present, each through money_text so the
        # symbol lands after the number, and a ruled total rather than cards.
        check("/me shows every balance figure through money_text",
              all(f"<span>{mt(v)}</span>" in me_text
                  for v in (bal.available, bal.held, bal.coins)),
              me_text[me_text.find("Your money"):][:300])
        # Not `"stat" not in me_text` -- that matched the orders table's own
        # Status column. A substring check is only as good as the substring.
        check("/me balances are a ruled list with a total, not stat cards",
              'class="sums"' in me_text and 'class="row total"' in me_text,
              "expected the definition-list shape from the accepted brief")
        check("/me never prints the retired currency word",
              not RETIRED.search(me_text),
              str(RETIRED.findall(me_text)[:5]))
        check("/me claim payout is not a bare number", "<td class=\"num\">300 g</td>" in me_text,
              "no '300 g' payout cell")
        check("/me history delta is signed and carries the symbol",
              "+500 g" in me_text and "+1,000 g" in me_text)
        check("/me history balance column carries the symbol", "1,500 g" in me_text)
        check("/me still marks every figure cell class=\"num\" (no restyle)",
              me_text.count('class="num') >= 6)

        identity_box["value"] = Identity(subject="u:2", discord_id="2",
                                         name="Owner", staff=True)
        r = await client.get("/ledger")
        check("/ledger answers 200 for staff", r.status == 200)
        led_text = await r.text()

        check("/ledger wallet balance cell carries the symbol",
              f'class="num">{bal.coins:,} g<' in led_text,
              led_text[led_text.find("Balances"):][:400])
        check("/ledger settlement amount is signed and carries the symbol",
              "+500 g" in led_text)
        check("/ledger audit money cell carries the symbol", "1,000 g" in led_text)
        check("/ledger never prints the retired currency word",
              not RETIRED.search(led_text),
              str(RETIRED.findall(led_text)[:5]))
        check("/ledger keeps its tabular figure cells (no restyle)",
              'class="num"' in led_text and "tablewrap" in led_text)


asyncio.run(main())

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("currency display: all checks pass")
