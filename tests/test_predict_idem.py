"""Prediction-panel idempotency test -- the double-clicked confirm button.

Standalone script, same shape as every other file in tests/: checks at
import, exit 1 on any failure. Run it with `python3 run_tests.py`, never
pytest (see run_tests.py's docstring for why).

The defect this file exists for: `_StakeConfirmGate.confirm` in
bot/views/predict.py neither disabled its button on the first click nor
keyed the stake placement on anything, so a double click during the DB
round trip escrowed 2x the intended amount against one preview -- real
player money locked up, and the pari-mutuel pool inflated with a phantom
stake, which makes the payout maths wrong for everyone in that market.

It drives the real view class with the discord stub, so it exercises the
actual button callback rather than a paraphrase of it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "_stubs"))

_tmp = tempfile.mkdtemp(prefix="nola-predict-idem-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("SHOP_CHANNEL_ID", "2")
os.environ.setdefault("ORDERS_CHANNEL_ID", "3")
os.environ.setdefault("ALERTS_CHANNEL_ID", "4")

import discord_stub  # noqa: E402
discord_stub.install()

from core import db, money, predictions       # noqa: E402
from bot.views import predict                 # noqa: E402

db.init_db()

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def reset() -> None:
    with db.db() as c:
        for t in ("pred_stakes", "pred_outcomes", "pred_markets", "game_bets",
                  "game_rounds", "ledger_entries", "ledger_holds", "wallet_flags",
                  "idempotency", "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:games", deficit_floor=1_000_000, service="owner")


def age_wallet(subject: str, days: int = 30) -> None:
    with db.db() as c:
        c.execute(
            "UPDATE wallets SET created_at = datetime('now', ?) WHERE subject = ?",
            (f"-{days} days", subject),
        )


# ------------------------------------------------------------------ fake interaction

class _Response:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.done = False

    async def defer(self, **_kw) -> None:
        self._log.append("defer")
        self.done = True

    async def send_message(self, content: str = "", **_kw) -> None:
        self._log.append(f"send_message:{content}")
        self.done = True

    async def edit_message(self, **_kw) -> None:
        self._log.append("edit_message")
        self.done = True

    async def send_modal(self, modal) -> None:
        self._log.append("send_modal")
        self.done = True


class _Followup:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def send(self, content: str = "", **_kw) -> None:
        self._log.append(f"followup:{content}")


class FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid


class FakeInteraction:
    """Enough of discord.Interaction for a button callback."""

    def __init__(self, uid: int, log: list[str]) -> None:
        self.user = FakeUser(uid)
        self.log = log
        self.response = _Response(log)
        self.followup = _Followup(log)
        self.message = None


class FakeButton:
    def __init__(self) -> None:
        self.disabled = False


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def stake_rows(subject: str) -> list:
    with db.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pred_stakes WHERE subject = ? ORDER BY id", (subject,))]


def open_holds(subject: str) -> list:
    with db.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM ledger_holds WHERE subject = ? AND state = 'open' ORDER BY id",
            (subject,))]


print("\n[1] one preview, two clicks -> one stake, one hold")
reset()
OWNER_ID = 424242
SUBJECT = money.user(OWNER_ID)
money.ensure_wallet(SUBJECT, service="owner")
money.mint(SUBJECT, 5_000, service="owner", reason="test funding")
age_wallet(SUBJECT)

mid = predictions.open_market("Will the levee hold?", ["yes", "no"],
                              created_by="tester")
with db.db() as _c:
    _outcomes = [dict(r) for r in _c.execute(
        "SELECT id, label FROM pred_outcomes WHERE market_id = ? ORDER BY id", (mid,))]
OUTCOME_ID, OUTCOME_LABEL = _outcomes[0]["id"], _outcomes[0]["label"]

market = {"id": mid, "question": "Will the levee hold?"}

# Build the gate exactly as _StakeAmountModal.on_submit does, through a real
# modal submit so the idempotency key is minted where the code mints it.
modal = predict._StakeAmountModal(market, OUTCOME_LABEL, SUBJECT,
                                  outcome_id=OUTCOME_ID) \
    if "outcome_id" in predict._StakeAmountModal.__init__.__code__.co_varnames \
    else predict._StakeAmountModal(market, OUTCOME_LABEL, SUBJECT)
modal.amount.value = "500"
mlog: list[str] = []
minter = FakeInteraction(OWNER_ID, mlog)
_sent: dict = {}

async def _capture(content: str = "", **kw):
    mlog.append("followup")
    _sent["view"] = kw.get("view")

minter.followup.send = _capture          # type: ignore[assignment]
run(modal.on_submit(minter))

gate = _sent.get("view")
check("modal submit produced a confirm gate", isinstance(gate, predict._StakeConfirmGate),
      f"got {type(gate).__name__}")

log: list[str] = []
btn = FakeButton()
run(gate.confirm(FakeInteraction(OWNER_ID, log), btn))
first_rows = stake_rows(SUBJECT)
check("first click placed exactly one stake", len(first_rows) == 1,
      f"{len(first_rows)} rows")

# The double click: the same gate object, clicked again before the user has
# any way to know the first one landed.
log2: list[str] = []
run(gate.confirm(FakeInteraction(OWNER_ID, log2), btn))

rows = stake_rows(SUBJECT)
holds = open_holds(SUBJECT)
check("double click leaves exactly ONE pred_stakes row", len(rows) == 1,
      f"{len(rows)} rows: {[r['amount'] for r in rows]}")
check("double click leaves exactly ONE open hold", len(holds) == 1,
      f"{len(holds)} holds: {[h['amount'] for h in holds]}")
check("only 500 g is escrowed, not 1000 g",
      sum(h["amount"] - h["captured"] - h["released"] for h in holds) == 500,
      f"escrowed {sum(h['amount'] - h['captured'] - h['released'] for h in holds)}")
bal = money.balance(SUBJECT)
check("available balance reflects one 500 g hold", bal.available == 4_500,
      f"available {bal.available}, held {bal.held}")

check("the button was disabled on the first click", btn.disabled is True)
check("the second click was answered, not silently dropped", len(log2) > 0,
      "no interaction response at all")

print("\n[2] the pool is not inflated by a phantom stake")
with db.db() as c:
    pool = c.execute("SELECT COALESCE(SUM(amount), 0) AS p FROM pred_stakes "
                     " WHERE market_id = ?", (mid,)).fetchone()["p"]
check("market pool is 500, not 1000", pool == 500, f"pool {pool}")

print("\n[3] the gate refuses a click from someone who is not its owner")
reset()
money.ensure_wallet(SUBJECT, service="owner")
money.mint(SUBJECT, 5_000, service="owner", reason="test funding")
age_wallet(SUBJECT)
mid2 = predictions.open_market("Second market?", ["yes", "no"], created_by="tester")
with db.db() as _c:
    _o2 = _c.execute("SELECT id, label FROM pred_outcomes WHERE market_id = ? ORDER BY id",
                     (mid2,)).fetchone()

modal2 = predict._StakeAmountModal({"id": mid2, "question": "Second market?"},
                                   _o2["label"], SUBJECT, outcome_id=_o2["id"]) \
    if "outcome_id" in predict._StakeAmountModal.__init__.__code__.co_varnames \
    else predict._StakeAmountModal({"id": mid2, "question": "Second market?"},
                                   _o2["label"], SUBJECT)
modal2.amount.value = "100"
mlog2: list[str] = []
minter2 = FakeInteraction(OWNER_ID, mlog2)
_sent2: dict = {}

async def _capture2(content: str = "", **kw):
    _sent2["view"] = kw.get("view")

minter2.followup.send = _capture2         # type: ignore[assignment]
run(modal2.on_submit(minter2))
gate2 = _sent2["view"]

intruder_log: list[str] = []
intruder = FakeInteraction(999_999, intruder_log)
allowed = run(gate2.interaction_check(intruder))
check("interaction_check refuses a stranger's click", allowed is False,
      f"returned {allowed!r}")
check("interaction_check answers the stranger", len(intruder_log) > 0)
allowed_owner = run(gate2.interaction_check(FakeInteraction(OWNER_ID, [])))
check("interaction_check allows the owner", allowed_owner is True,
      f"returned {allowed_owner!r}")
check("the stranger's click placed nothing", len(stake_rows(SUBJECT)) == 0,
      f"{len(stake_rows(SUBJECT))} rows")

print("\n[4] two DIFFERENT previews are two different stakes")
run(gate2.confirm(FakeInteraction(OWNER_ID, []), FakeButton()))
modal3 = predict._StakeAmountModal({"id": mid2, "question": "Second market?"},
                                   _o2["label"], SUBJECT, outcome_id=_o2["id"]) \
    if "outcome_id" in predict._StakeAmountModal.__init__.__code__.co_varnames \
    else predict._StakeAmountModal({"id": mid2, "question": "Second market?"},
                                   _o2["label"], SUBJECT)
modal3.amount.value = "100"
_sent3: dict = {}

async def _capture3(content: str = "", **kw):
    _sent3["view"] = kw.get("view")

m3 = FakeInteraction(OWNER_ID, [])
m3.followup.send = _capture3              # type: ignore[assignment]
run(modal3.on_submit(m3))
run(_sent3["view"].confirm(FakeInteraction(OWNER_ID, []), FakeButton()))
check("a second, separately previewed stake DOES place a second row",
      len(stake_rows(SUBJECT)) == 2, f"{len(stake_rows(SUBJECT))} rows")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all predict idempotency checks passed")
