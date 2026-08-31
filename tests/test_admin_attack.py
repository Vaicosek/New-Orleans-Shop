"""Adversarial tests against the admin surface: minting, the add-item modal
and the restock-alert scan.

Same shape as tests/test_orders_attack.py -- real temp SQLite, check() /
raises() -- plus the `discord` stub from tests/_stubs so `bot/` can be
imported and its views actually DRIVEN (modals submitted, buttons clicked)
without a gateway connection.

Three defects are pinned here:

  [1] treasury funding minted with no idempotency key behind a re-clickable
      confirm gate: one approved 10,000 g funding became 20,000 g on a
      double click, and the two audit rows shared no action_key so the
      duplicate read as two legitimate fundings.
  [2] the "Add item" modal collapsed price_unit_pieces into stack_size, so
      the sapling (1 g per 32 pieces, stack size 64) was unrepresentable and
      every full stack was silently half priced.
  [3] one exception inside the restock scan killed the loop for the life of
      the process, with a boot self-check that still printed OK.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "_stubs"))

_tmp = tempfile.mkdtemp(prefix="nola-admin-attack-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("SHOP_CHANNEL_ID", "2")
os.environ.setdefault("ORDERS_CHANNEL_ID", "3")
os.environ.setdefault("ALERTS_CHANNEL_ID", "4")

import discord_stub  # noqa: E402
discord_stub.install()

from core import catalog, db, money, pricing                     # noqa: E402
from bot.views import admin as admin_views                       # noqa: E402
from bot.cogs import admin as admin_cog                          # noqa: E402

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
                  "audit_actions", "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")


db.init_db()
reset()


# ------------------------------------------------------------------ fakes

class Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object, dict]] = []
        self.modals: list[object] = []
        self.edits: list[dict] = []


class _Response:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    async def defer(self, **_kw) -> None:
        return None

    async def send_message(self, content=None, **kw) -> None:
        self.rec.messages.append(("response", content, kw))

    async def edit_message(self, **kw) -> None:
        self.rec.edits.append(kw)

    async def send_modal(self, modal) -> None:
        self.rec.modals.append(modal)


class _Followup:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    async def send(self, content=None, **kw) -> None:
        self.rec.messages.append(("followup", content, kw))


class _Message:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    async def edit(self, **kw) -> None:
        self.rec.edits.append(kw)


class FakeInteraction:
    def __init__(self, rec: Recorder, user_id: int = 4242) -> None:
        self.response = _Response(rec)
        self.followup = _Followup(rec)
        self.message = _Message(rec)
        self.user = types.SimpleNamespace(id=user_id, roles=[])
        self.client = types.SimpleNamespace(nola_config=None)
        self.id = 999


class FakeButton:
    def __init__(self) -> None:
        self.disabled = False


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


asyncio.set_event_loop(asyncio.new_event_loop())


def audit_rows(kind: str) -> list[dict]:
    with db.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM audit_actions WHERE kind = ? ORDER BY id", (kind,)).fetchall()]


# ================================================================== [1] mint
print("\n[1] treasury funding: one approval, one mint")

SUBJECT = "treasury:house"
LABEL = money.TREASURY_NAMES[SUBJECT]
FUNDER = "user:4242"
CONFIG = types.SimpleNamespace(owner_discord_ids=[4242])

rec = Recorder()
amount_modal = admin_views._FundAmountModal(SUBJECT, FUNDER, CONFIG)
amount_modal.amount.value = "10000"
run(amount_modal.on_submit(FakeInteraction(rec)))

gate = None
for _where, _content, kw in rec.messages:
    if kw.get("view") is not None:
        gate = kw["view"]
check("the amount modal previews the figures and hands back a confirm gate",
      gate is not None)

# Two clicks on the same gate -- a double click, or Discord retrying the
# interaction. Both are ordinary, and neither is a second approval.
if gate is not None:
    run(gate.confirm(FakeInteraction(rec), FakeButton()))
    run(gate.confirm(FakeInteraction(rec), FakeButton()))

check("a second click on a consumed confirm gate opens no second modal "
      f"(opened {len(rec.modals)})", len(rec.modals) == 1)

# Submit every modal the gate did hand out, twice each: the confirm modal is
# itself re-submittable, and the typed name is the same both times.
for modal in list(rec.modals):
    for _ in range(2):
        modal.confirm.value = LABEL
        run(modal.on_submit(FakeInteraction(rec)))

balance = money.balance(SUBJECT).coins
check(f"one approved funding of 10,000 {pricing.CURRENCY} minted exactly once "
      f"(balance {balance})", balance == 10_000)

rows = audit_rows("treasury.fund")
check(f"exactly one treasury.fund audit row ({len(rows)})", len(rows) == 1)
check("that audit row carries an action_key, so a duplicate could never read "
      "as a second legitimate funding",
      bool(rows and rows[0]["action_key"]))
check("the audit action_key is the money claim's own key, not a rebuilt one",
      bool(rows) and str(rows[0]["action_key"]).startswith(
          f"treasury.fund:{SUBJECT}:10000:"))

with db.db() as c:
    idem = [dict(r) for r in c.execute(
        "SELECT * FROM idempotency WHERE endpoint = 'treasury.fund'").fetchall()]
check(f"the mint claimed exactly one idempotency key, resolved done ({len(idem)})",
      len(idem) == 1 and idem[0]["state"] == "done")

# A SECOND, genuinely separate approval must still work -- idempotency that
# blocks the next real funding is its own outage.
rec2 = Recorder()
m2 = admin_views._FundAmountModal(SUBJECT, FUNDER, CONFIG)
m2.amount.value = "10000"
run(m2.on_submit(FakeInteraction(rec2)))
gate2 = next(kw["view"] for _w, _c, kw in rec2.messages if kw.get("view") is not None)
run(gate2.confirm(FakeInteraction(rec2), FakeButton()))
for modal in rec2.modals:
    modal.confirm.value = LABEL
    run(modal.on_submit(FakeInteraction(rec2)))
check(f"a separate second approval funds again (balance {money.balance(SUBJECT).coins})",
      money.balance(SUBJECT).coins == 20_000)
check("and leaves two distinct audit rows", len(audit_rows("treasury.fund")) == 2)


# ================================================================== [2] item
print("\n[2] add-item modal: price unit and stack size are two numbers")

reset()
rec = Recorder()
add = admin_views._AddItemModal()
add.name.value = "Oak Sapling"
add.price.value = "1"
add.unit_pieces.value = "32"          # 1 g buys 32 pieces
add.stack_size.value = "64"           # a full stack is 64
add.barrel_slots.value = "54"
run(add.on_submit(FakeInteraction(rec)))

item = catalog.list_items(active_only=False)
item = item[0] if item else None
check("the sapling case is representable through the modal", item is not None)
if item is not None:
    check(f"price_unit_pieces stayed 32 (got {item['price_unit_pieces']})",
          item["price_unit_pieces"] == 32)
    check(f"stack_size stayed 64 (got {item['stack_size']})", item["stack_size"] == 64)
    full_stack = pricing.charge(64, item["price_coins"], item["price_unit_pieces"])
    check(f"a full stack costs 2 {pricing.CURRENCY}, not 1 (got {full_stack})",
          full_stack == 2)

# The contract's rule, surfaced rather than re-derived.
rec = Recorder()
bad = admin_views._AddItemModal()
bad.name.value = "Impossible"
bad.price.value = "1"
bad.unit_pieces.value = "128"
bad.stack_size.value = "64"
bad.barrel_slots.value = "54"
run(bad.on_submit(FakeInteraction(rec)))
said = " ".join(str(c) for _w, c, _k in rec.messages)
check("price_unit_pieces above stack_size is refused, with the reason shown",
      "price_unit_pieces must not exceed stack_size" in said)
check("and nothing was added", len(catalog.list_items(active_only=False)) == 1)


# ================================================================== [3] scan
print("\n[3] restock scan: one bad item skips one item, never the process")


class Channel:
    def __init__(self, fail_ids: set[int]) -> None:
        self.fail_ids = fail_ids
        self.sent: list[int] = []
        self.attempted: list[int] = []

    async def send(self, *, embed=None, **_kw) -> None:
        item_id = embed
        self.attempted.append(item_id)
        if item_id in self.fail_ids:
            raise RuntimeError("Discord said no")
        self.sent.append(item_id)


DUE = [{"item_id": 1, "name": "a"}, {"item_id": 2, "name": "b"}, {"item_id": 3, "name": "c"}]

_orig_layout = admin_cog.layout
_orig_embed = admin_cog.build_alert_embed
_orig_view = admin_cog.AlertAckView
_orig_alerts = admin_cog.alerts

channel = Channel(fail_ids={2})
admin_cog.layout = types.SimpleNamespace(channel=lambda *_a, **_kw: channel)
admin_cog.build_alert_embed = lambda row: row["item_id"]
admin_cog.AlertAckView = lambda *a, **kw: None
admin_cog.alerts = types.SimpleNamespace(due=lambda: list(DUE))

bot_obj = types.SimpleNamespace(nola_config=types.SimpleNamespace(guild_id=1))
cog = admin_cog.AdminCog(bot_obj)

health_before = cog.scan_health() if hasattr(cog, "scan_health") else ""
check("before any scan, health does NOT report OK on the strength of a "
      f"channel that resolved once ({health_before!r})",
      "NO SUCCESSFUL SCAN YET" in health_before)

crashed = None
try:
    run(cog.scan_alerts.coro(cog))
except Exception as err:                                          # noqa: BLE001
    crashed = err
check(f"one failing item does not raise out of the scan ({crashed!r})", crashed is None)
check(f"every due item was attempted ({channel.attempted})",
      channel.attempted == [1, 2, 3])
check(f"the two healthy items still posted ({channel.sent})", channel.sent == [1, 3])
check("the failure is recorded rather than swallowed",
      bool(getattr(cog, "last_scan_error", None)) and "2" in str(cog.last_scan_error))
check("the health signal is the LAST SUCCESSFUL SCAN, and now reports one",
      getattr(cog, "last_scan_ok_at", None) is not None
      and "last success" in cog.scan_health())

check("the loop has an error handler, so a crash restarts it instead of "
      "dying for the life of the process",
      hasattr(admin_cog.AdminCog, "scan_alerts_error"))
if hasattr(admin_cog.AdminCog, "scan_alerts_error"):
    run(cog.scan_alerts_error(RuntimeError("gateway went away")))
    check("the crash handler records why", "loop crashed" in str(cog.last_scan_error))

admin_cog.layout = _orig_layout
admin_cog.build_alert_embed = _orig_embed
admin_cog.AlertAckView = _orig_view
admin_cog.alerts = _orig_alerts

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all admin attack tests pass")
