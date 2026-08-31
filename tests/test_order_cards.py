"""Adversarial tests against the PUBLIC cards: the order card, the /go
round card, and the restock alert card.

Same shape as tests/test_admin_attack.py -- real temp SQLite, check() /
raises(), plus the `discord` stub from tests/_stubs so `bot/` can be
imported and its views actually DRIVEN (modals submitted, buttons clicked)
without a gateway connection.

Five defects are pinned here:

  [1] claiming from the /orders panel never refreshed the public order card:
      the refresh was keyed on `interaction.message`, which is the
      embed-less picker, so the channel board kept reporting "open, no
      claims" on an order two people were already working.
  [2] approving from the panel likewise left a paid, fulfilled order
      printed as awaiting_verification with a LIVE Approve button.
  [3] a non-positive piece count raised ValueError out of the modal AFTER
      the deferral, so the user got a silent failure and no message at all.
  [4] `/go <round code>` attached a Verify button to an embed with no
      footer, and the button re-resolves its round FROM the footer -- so the
      one UI element offered for checking a suspicious result could never
      resolve anything.
  [5] the alert card's Acknowledge button had no staff check, so any viewer
      could permanently silence a restock alert, at 0 stock, with no audit
      row naming who did it.
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

_tmp = tempfile.mkdtemp(prefix="nola-order-cards-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("SHOP_CHANNEL_ID", "2")
os.environ.setdefault("ORDERS_CHANNEL_ID", "3")
os.environ.setdefault("ALERTS_CHANNEL_ID", "4")

import discord_stub  # noqa: E402
discord_stub.install()

from core import alerts, catalog, db, money, orders as orders_core   # noqa: E402
from bot import addressing                                          # noqa: E402
from bot.views import alerts as alert_views                         # noqa: E402
from bot.views import casino as casino_views                        # noqa: E402
from bot.views import orders as order_views                         # noqa: E402
from bot.cogs import go as go_cog                                   # noqa: E402

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
                  "audit_actions", "addresses", "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")


db.init_db()
reset()

CARD_CHANNEL = 3
CARD_MESSAGE = 777_000
STAFF_ROLE = 55
CONFIG = types.SimpleNamespace(staff_role_ids=[STAFF_ROLE], manager_role_ids=[],
                                owner_discord_ids=[])


# ------------------------------------------------------------------ fakes

class FakeCardMessage:
    """The PUBLIC card sitting in the orders channel. Nobody's interaction
    is ON this message in the panel flow -- it can only be reached through
    the channel/message ids stored on the order row."""

    def __init__(self, message_id: int = CARD_MESSAGE) -> None:
        self.id = message_id
        self.embeds: list = []
        self.edits: list[dict] = []

    async def edit(self, **kw):
        self.edits.append(kw)
        if kw.get("embed") is not None:
            self.embeds = [kw["embed"]]
        return None

    def body(self) -> str:
        return self.embeds[0].description if self.embeds else ""


class FakeChannel:
    def __init__(self, message: FakeCardMessage) -> None:
        self.message = message

    async def fetch_message(self, message_id: int):
        if int(message_id) != int(self.message.id):
            raise KeyError(message_id)
        return self.message


class Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object, dict]] = []
        self.modals: list[object] = []
        self.edits: list[dict] = []

    def texts(self) -> list[str]:
        return [str(c) for _w, c, _kw in self.messages if c is not None]


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


class _PickerMessage:
    """The ephemeral, embed-LESS picker message the panel flow is actually
    on. `parse_order_id` can never recover an order from this."""

    def __init__(self) -> None:
        self.id = 111_222
        self.embeds: list = []
        self.edits: list[dict] = []

    async def edit(self, **kw):
        self.edits.append(kw)
        return None


class FakeInteraction:
    def __init__(self, rec: Recorder, *, message=None, user_id: int = 4242,
                 roles: list[int] | None = None, channel=None, config=CONFIG) -> None:
        self.response = _Response(rec)
        self.followup = _Followup(rec)
        self.message = message if message is not None else _PickerMessage()
        self.user = types.SimpleNamespace(
            id=user_id, roles=[types.SimpleNamespace(id=r) for r in (roles or [])])
        self.client = types.SimpleNamespace(
            nola_config=config,
            get_channel=lambda _cid: channel,
            fetch_channel=_unavailable,
        )
        self.channel_id = CARD_CHANNEL
        self.id = 999


async def _unavailable(_cid):
    raise RuntimeError("fetch_channel not available in tests")


class FakeButton:
    def __init__(self) -> None:
        self.disabled = False


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


asyncio.set_event_loop(asyncio.new_event_loop())


def make_order(pieces: int = 64) -> int:
    money.ensure_wallet("treasury:shop", deficit_floor=10_000_000, service="owner")
    item_id = catalog.add_item("Cobblestone", 640, price_unit_pieces=64,
                                stack_size=64, barrel_slots=54)
    order_id = orders_core.create_order(item_id, pieces, created_by="u:owner")
    orders_core.set_message(order_id, str(CARD_CHANNEL), str(CARD_MESSAGE))
    return order_id


def audit_rows(kind: str) -> list[dict]:
    with db.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM audit_actions WHERE kind = ? ORDER BY id", (kind,)).fetchall()]


# =============================================== [1] claim from the panel
print("\n[1] claiming from the /orders panel refreshes the PUBLIC card")

reset()
order_id = make_order()
card = FakeCardMessage()
card.embeds = [order_views.build_order_embed(order_id)]
rec = Recorder()

modal = order_views._PiecesModal("Claim pieces", order_id, "u:9001", "claim")
modal.pieces.value = "64"
run(modal.on_submit(FakeInteraction(rec, channel=FakeChannel(card))))

check("the claim itself landed",
      orders_core.get_order(order_id)["status"] == "claimed")
check(f"the public order card was edited after a panel claim "
      f"({len(card.edits)} edit(s))", len(card.edits) == 1)
check("the refreshed card no longer reports the order as open",
      f"Status: {order_views.status_label('claimed')}" in card.body(), card.body())
check("the refreshed card names the claim, so a second worker sees it is taken",
      order_views.worker_mention("u:9001") in card.body(), card.body())


# ============================================= [1b] deliver from the panel
print("\n[1b] marking delivered from the panel refreshes the PUBLIC card")

card.edits.clear()
rec = Recorder()
modal = order_views._PiecesModal("Pieces delivered", order_id, "u:9001", "deliver")
modal.pieces.value = "64"
run(modal.on_submit(FakeInteraction(rec, channel=FakeChannel(card))))

check("the delivery itself landed",
      orders_core.get_order(order_id)["status"] == "awaiting_verification")
check(f"the public order card was edited after a panel delivery "
      f"({len(card.edits)} edit(s))", len(card.edits) == 1)
check("the refreshed card reports awaiting_verification",
      f"Status: {order_views.status_label('awaiting_verification')}" in card.body(), card.body())


# ================================================ [2] approve from the panel
print("\n[2] approving from the /orders panel refreshes the PUBLIC card")

card.edits.clear()
rec = Recorder()
# Constructed exactly as bot/views/orders.py's approve-queue picker builds
# it: no origin channel/message, because the panel never had them.
preview = orders_core.preview_approval(order_id, "u:4242")
gate = order_views._ApproveGate(order_id, "u:4242", total_coins=preview["total_coins"])
run(gate.confirm(FakeInteraction(rec, roles=[STAFF_ROLE], channel=FakeChannel(card)),
                 FakeButton()))

check("the approval itself landed and paid",
      orders_core.get_order(order_id)["status"] == "fulfilled")
check(f"the public order card was edited after a panel approval "
      f"({len(card.edits)} edit(s))", len(card.edits) == 1)
check("the refreshed card reports fulfilled, not awaiting_verification",
      f"Status: {order_views.status_label('fulfilled')}" in card.body(), card.body())
_view = card.edits[-1].get("view") if card.edits else None
check("every button on the refreshed card of a closed order is disabled -- "
      "no live Approve on an already-paid order",
      _view is not None and all(getattr(c, "disabled", False) for c in _view.children))


# ================================================= [3] a bad piece count
print("\n[3] a non-positive piece count gets a real answer, not silence")

reset()
order_id = make_order()
card = FakeCardMessage()
card.embeds = [order_views.build_order_embed(order_id)]

for bad in ("0", "-5"):
    rec = Recorder()
    modal = order_views._PiecesModal("Claim pieces", order_id, "u:9001", "claim")
    modal.pieces.value = bad
    err = None
    try:
        run(modal.on_submit(FakeInteraction(rec, channel=FakeChannel(card))))
    except Exception as e:                                       # noqa: BLE001
        err = e
    check(f"pieces={bad}: nothing escapes the modal after the deferral "
          f"({type(err).__name__ if err else 'clean'})", err is None,
          repr(err))
    check(f"pieces={bad}: the user is told why", bool(rec.texts()),
          f"messages={rec.texts()}")
    check(f"pieces={bad}: no claim was created",
          orders_core.list_claims(order_id) == [])


# =============================================== [4] /go on a round code
print("\n[4] /go on a round code attaches a Verify button that can resolve")

reset()
ROUND_ID = "round.dice:deadbeefdeadbeef"
code = addressing.mint("game_round", ROUND_ID)
rec = Recorder()
cog = go_cog.GoCog(types.SimpleNamespace())
run(go_cog.GoCog.go.callback(cog, FakeInteraction(rec), code))

sent = [kw for _w, _c, kw in rec.messages if kw.get("embed") is not None]
check("/go on a round code sends an embed with a Verify view",
      bool(sent) and sent[-1].get("view") is not None)
embed = sent[-1]["embed"] if sent else None
footer_text = getattr(getattr(embed, "footer", None), "text", None)
check("that embed carries a footer at all", bool(footer_text), repr(footer_text))

card_msg = types.SimpleNamespace(embeds=[embed] if embed is not None else [])
check("the Verify button's own resolver recovers this round from that footer",
      casino_views.parse_round_id(card_msg) == ROUND_ID,
      f"footer={footer_text!r} parsed={casino_views.parse_round_id(card_msg)!r}")
check("the footer still shows the address code, not a bare internal id",
      bool(footer_text) and code in footer_text, repr(footer_text))


# ============================================ [5] alert Acknowledge is staff
print("\n[5] the restock Acknowledge button is staff-gated and audited")

reset()
item_id = catalog.add_item("Sapling", 1, price_unit_pieces=32, stack_size=64,
                            barrel_slots=1)
catalog.set_stock(item_id, 0)
alerts.set_threshold(item_id, threshold_pieces=32)
due = alerts.due()
check("the item is genuinely a due alert at 0 stock", len(due) == 1)

alert_card = FakeCardMessage(message_id=888_111)
alert_card.embeds = [alert_views.build_alert_embed(due[0])]
view = alert_views.AlertAckView()

# A random member who can merely SEE the card.
rec = Recorder()
inter = FakeInteraction(rec, message=alert_card, user_id=1234, roles=[])
allowed = run(view.interaction_check(inter))
if allowed:
    run(view.ack_btn(inter, FakeButton()))

with db.db() as c:
    acked = c.execute("SELECT acked_until_qty FROM stock_alerts WHERE item_id = ?",
                      (item_id,)).fetchone()["acked_until_qty"]
check("a non-staff member cannot acknowledge a restock alert",
      allowed is False and acked is None,
      f"allowed={allowed} acked_until_qty={acked}")
check("...and is told why", bool(rec.texts()), f"messages={rec.texts()}")
check("stock at 0 is still a due alert after a refused acknowledgement",
      len(alerts.due()) == 1)

# Staff.
rec = Recorder()
inter = FakeInteraction(rec, message=alert_card, user_id=4242, roles=[STAFF_ROLE])
allowed = run(view.interaction_check(inter))
check("a staff member passes the gate", allowed is True)
if allowed:
    run(view.ack_btn(inter, FakeButton()))

with db.db() as c:
    acked = c.execute("SELECT acked_until_qty FROM stock_alerts WHERE item_id = ?",
                      (item_id,)).fetchone()["acked_until_qty"]
check(f"acknowledging at 0 stock stores the real quantity, zero ({acked})",
      acked == 0,
      "flooring this to 1 makes the ack a no-op: due() fires on qty < acked_until_qty, "
      "and 0 < 1 is true on every scan")
check("the alert actually goes quiet -- this is the AbexTech repeating-DM bug "
      "CONTRACT section 6 exists to kill", len(alerts.due()) == 0,
      f"still due: {alerts.due()}")

# ...and it is not silenced forever: the reset in due() clears the suppression
# once the item is restocked above threshold, so a LATER dip speaks again.
with db.db() as c:
    c.execute("UPDATE stock SET pieces = 9999 WHERE item_id = ?", (item_id,))
check("restocking above threshold clears the acknowledgement",
      len(alerts.due()) == 0)
with db.db() as c:
    cleared = c.execute("SELECT acked_until_qty FROM stock_alerts WHERE item_id = ?",
                        (item_id,)).fetchone()["acked_until_qty"]
check("acked_until_qty is reset to NULL by the restock", cleared is None,
      f"acked_until_qty={cleared}")
with db.db() as c:
    c.execute("UPDATE stock SET pieces = 0 WHERE item_id = ?", (item_id,))
check("a later dip fires the alert again, from a clean slate",
      len(alerts.due()) == 1)

rows = audit_rows("alert.ack")
check(f"exactly one alert.ack audit row ({len(rows)})", len(rows) == 1)
check("the audit row names the actor who silenced it",
      bool(rows) and rows[0]["actor"] == money.user(4242),
      f"actor={rows[0]['actor'] if rows else None}")


# ------------------------------------------------------------------ verdict

# --------------------------------------------------------------- /setup roles
# /setup creates Staff and Manager and stores their ids. Before this worked,
# it was a trap: the roles existed, people held them, and every permission
# check still said no -- because is_staff read STAFF_ROLE_IDS from the
# environment, which is empty on a server that was set up rather than
# hand-wired. Same chicken-and-egg as the channel ids; fixed there, missed here.
print()
print("permissions fall back to the roles /setup provisioned")

from core import provision as _prov  # noqa: E402
from bot import permissions as _perms  # noqa: E402


class _Role:
    def __init__(self, rid): self.id = rid


class _Guild:
    def __init__(self, gid): self.id = gid


class _Member:
    def __init__(self, gid, role_ids):
        self.guild = _Guild(gid)
        self.roles = [_Role(r) for r in role_ids]


_GID = 55501
_prov.record(_GID, "role:staff", 900001, "Staff")
_prov.record(_GID, "role:manager", 900002, "Manager")


class _EmptyCfg:
    staff_role_ids = ()
    manager_role_ids = ()
    owner_discord_ids = ()


check("a holder of the provisioned Staff role is staff, with NO env ids set",
      _perms.is_staff(_Member(_GID, [900001]), _EmptyCfg()) is True,
      "this is what makes /admin openable after /setup")
check("a holder of the provisioned Manager role is manager",
      _perms.is_manager(_Member(_GID, [900002]), _EmptyCfg()) is True)
check("somebody holding neither is still refused",
      _perms.is_staff(_Member(_GID, [123456]), _EmptyCfg()) is False)
check("a member of a DIFFERENT guild is refused -- the lookup is per-guild",
      _perms.is_staff(_Member(_GID + 1, [900001]), _EmptyCfg()) is False)


class _EnvCfg:
    staff_role_ids = (777001,)
    manager_role_ids = ()
    owner_discord_ids = ()


check("an explicit STAFF_ROLE_IDS still wins over the provisioned role",
      _perms.is_staff(_Member(_GID, [777001]), _EnvCfg()) is True
      and _perms.is_staff(_Member(_GID, [900001]), _EnvCfg()) is False,
      "env pins the answer; the table only fills a gap")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: " + ", ".join(FAILS))
    raise SystemExit(1)
print("test_order_cards: all checks passed")
