"""Betting tests -- casino and pari-mutuel. Real SQLite files, no mocks.

Same style as test_money.py: check()/raises(), exit 1 on any failure.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-bet-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import db, money, games, predictions              # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def raises(name: str, exc, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc:
        print(f"  ok    {name}")
    except Exception as err:                                 # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL  {name}  raised {type(err).__name__}: {err}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  did not raise {exc.__name__}")


def reset() -> None:
    with db.db() as c:
        for t in ("pred_stakes", "pred_outcomes", "pred_markets", "game_bets",
                  "game_rounds", "ledger_entries", "ledger_holds", "wallet_flags",
                  "idempotency", "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:games", deficit_floor=1_000_000, service="owner")


def age_wallet(subject: str, days: int = 30) -> None:
    """Push `subject`'s wallet past MIN_ACCOUNT_AGE_DAYS so a test can
    exercise something other than the account-age guard itself -- both
    games.place_bet and predictions.stake enforce it now (core/wagering.py)."""
    with db.db() as c:
        c.execute(
            "UPDATE wallets SET created_at = datetime('now', ?) WHERE subject = ?",
            (f"-{days} days", subject),
        )


db.init_db()

# ------------------------------------------------------------------ coinflip end to end
print("\ncoinflip: full round end to end")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
with db.db() as c:
    c.execute("UPDATE wallets SET created_at = datetime('now', '-30 days') WHERE subject = 'u:1'")

result = games.play("u:1", "coinflip", "heads", 100, client_seed="alice-seed", nonce=1)
check("play returns a round id and outcome", "round_id" in result and "outcome" in result)
bal = money.balance("u:1")
if result["results"][0]["win"]:
    check("winner's balance grew by (payout - stake)", bal.coins == 1000 + (result["results"][0]["payout"] - 100),
          f"coins={bal.coins}")
else:
    check("loser's balance shrank by the stake", bal.coins == 900, f"coins={bal.coins}")
check("nothing stays held after settlement", bal.held == 0)

round_row_game = result["game"]
check("round records the right game", round_row_game == "coinflip")

# ------------------------------------------------------------------ verify()
print("\nverify() reproduces the outcome")
v = games.verify(result["round_id"])
check("verify confirms the seed matches its commitment", v["seed_matches_commitment"])
check("verify recomputes the same outcome", v["outcome_matches"])
check("verify is overall ok", v["ok"])

raises("cannot verify an unsettled round", games.RoundNotOpen,
       games.verify, games.open_round("dice", "x", 0))

# tamper check: verify must actually catch a mismatch, not just always say ok
import json as _json
with db.db() as c:
    c.execute("UPDATE game_rounds SET outcome_json = ? WHERE id = ?",
              (_json.dumps({"face": "nonsense"}), result["round_id"]))
tampered = games.verify(result["round_id"])
check("verify catches a tampered outcome", not tampered["outcome_matches"] and not tampered["ok"])

# ------------------------------------------------------------------ settle twice
print("\nsettling the same round twice pays exactly once")
reset()
money.mint("u:1", 1000, service="owner", reason="seed")
with db.db() as c:
    c.execute("UPDATE wallets SET created_at = datetime('now', '-30 days') WHERE subject = 'u:1'")

round_id = games.open_round("dice", "bob-seed", 5)
games.place_bet(round_id, "u:1", "3", 100)
r1 = games.settle_round(round_id)
after_first = money.balance("u:1").coins
r2 = games.settle_round(round_id)                             # must be a no-op
after_second = money.balance("u:1").coins
check("second settle_round call changes nothing", after_first == after_second,
      f"{after_first} vs {after_second}")
check("second call reports no newly-settled bets", r2["results"] == [])
with db.db() as c:
    n = c.execute(
        "SELECT COUNT(*) AS n FROM game_bets WHERE round_id = ? AND settled_event IS NOT NULL",
        (round_id,),
    ).fetchone()["n"]
check("exactly one settled row exists, not two payouts", n == 1)

# ------------------------------------------------------------------ house edge is what's documented
print("\nhouse edge matches GAME_CONFIG")
check("coinflip pays 1.96x (2% edge on a fair 2.00x)",
      games.GAME_CONFIG["coinflip"]["payout_bps"] == 19_600)
check("dice pays 5.70x (5% edge on a fair 6.00x)",
      games.GAME_CONFIG["dice"]["payout_bps"] == 57_000)

# ------------------------------------------------------------------ gambling_blocked
print("\ngambling_blocked wallet cannot place a bet")
reset()
money.mint("u:2", 1000, service="owner", reason="seed")
with db.db() as c:
    c.execute("UPDATE wallets SET created_at = datetime('now', '-30 days') WHERE subject = 'u:2'")
money.set_flag("u:2", "gambling_blocked", service="owner", set_by="owner")
rid = games.open_round("coinflip", "x", 0)
raises("place_bet refuses a self-excluded wallet", money.GamblingBlocked,
       games.place_bet, rid, "u:2", "heads", 50)
check("no hold was left behind", money.balance("u:2").held == 0)

# ------------------------------------------------------------------ MAX_BET
print("\nMAX_BET refuses an oversized bet")
reset()
money.mint("u:3", 1_000_000, service="owner", reason="seed")
with db.db() as c:
    c.execute("UPDATE wallets SET created_at = datetime('now', '-30 days') WHERE subject = 'u:3'")
rid = games.open_round("coinflip", "x", 0)
raises("a bet over MAX_BET is refused", games.BetTooLarge,
       games.place_bet, rid, "u:3", "heads", games.MAX_BET + 1)
check("no hold placed for the refused bet", money.balance("u:3").held == 0)
games.place_bet(rid, "u:3", "heads", games.MAX_BET)            # exactly at the cap: fine
check("exactly MAX_BET is accepted", money.balance("u:3").held == games.MAX_BET)

# ------------------------------------------------------------------ MAX_DAILY_LOSS
print("\nMAX_DAILY_LOSS refuses once the cap is at risk")
reset()
money.mint("u:4", 1_000_000, service="owner", reason="seed")
with db.db() as c:
    c.execute("UPDATE wallets SET created_at = datetime('now', '-30 days') WHERE subject = 'u:4'")
    c.execute(
        "INSERT INTO gambling_day (subject, day, staked, lost) "
        "VALUES ('u:4', strftime('%Y-%m-%d','now'), 0, ?)",
        (games.MAX_DAILY_LOSS - 10,),
    )
rid = games.open_round("dice", "x", 0)
raises("a bet that could exceed the daily loss cap is refused", games.DailyLossExceeded,
       games.place_bet, rid, "u:4", "1", 11)
games.place_bet(rid, "u:4", "1", 10)                           # exactly at the remaining room
check("a bet within remaining room is accepted", money.balance("u:4").held == 10)

# ------------------------------------------------------------------ MIN_ACCOUNT_AGE_DAYS
print("\nMIN_ACCOUNT_AGE_DAYS refuses a brand-new wallet")
reset()
money.mint("u:5", 1000, service="owner", reason="seed")        # created_at defaults to now
rid = games.open_round("coinflip", "x", 0)
raises("a brand-new wallet cannot place a wager", games.AccountTooNew,
       games.place_bet, rid, "u:5", "heads", 10)

# ------------------------------------------------------------------ treasury:games can never mint
print("\ntreasury:games can never mint")
raises("games service has no MINT scope", money.NotPermitted,
       money.mint, "treasury:games", 100, service="games", reason="free money")
raises("games service cannot mint to a player either", money.NotPermitted,
       money.mint, "u:1", 100, service="games", reason="free money")
import inspect                                                 # noqa: E402
src = inspect.getsource(games) + inspect.getsource(predictions)
check("neither betting module calls money.mint anywhere", "money.mint(" not in src)

# ================================================================== pari-mutuel

# ------------------------------------------------------------------ full lifecycle + rake
print("\npari-mutuel: stake, close, resolve, pro-rata payout")
reset()
money.mint("u:10", 1000, service="owner", reason="seed")
money.mint("u:11", 1000, service="owner", reason="seed")
money.mint("u:12", 1000, service="owner", reason="seed")
age_wallet("u:10")
age_wallet("u:11")
age_wallet("u:12")

mkt = predictions.open_market("Who wins?", ["yes", "no"], created_by="owner", rake_bps=500)
predictions.stake(mkt, "u:10", "yes", 300)
predictions.stake(mkt, "u:11", "yes", 100)
predictions.stake(mkt, "u:12", "no", 400)
predictions.close(mkt)

event = money.new_event_id("pred.resolve")
res = predictions.resolve(mkt, "yes", event)

pool = 800
rake = pool * 500 // 10_000  # 40
distributable = pool - rake  # 760
expect_10 = distributable * 300 // 400  # 570
expect_11 = distributable * 100 // 400  # 190

check("pool is the sum of every stake", res["pool"] == pool)
check("rake matches rake_bps", res["rake"] == rake)
check("winner u:10 paid pro-rata", money.balance("u:10").coins == 1000 - 300 + expect_10,
      f"{money.balance('u:10').coins}")
check("winner u:11 paid pro-rata", money.balance("u:11").coins == 1000 - 100 + expect_11,
      f"{money.balance('u:11').coins}")
check("loser u:12 keeps nothing from their stake", money.balance("u:12").coins == 600)
check("invariant: payouts + rake + remainder == pool",
      res["paid_out"] + res["rake"] + res["remainder"] == pool)

# resolving again with the SAME event id is a safe replay
res2 = predictions.resolve(mkt, "yes", event)
check("replay with the same event id changes nothing",
      money.balance("u:10").coins == 1000 - 300 + expect_10)
check("replay reports the same summary", res2["paid_out"] == res["paid_out"])

raises("resolving again with a DIFFERENT event id is refused", predictions.AlreadyResolved,
       predictions.resolve, mkt, "no", money.new_event_id("pred.resolve"))

# ------------------------------------------------------------------ nobody backed the winner
print("\npari-mutuel: nobody staked the winning outcome")
reset()
money.mint("u:20", 500, service="owner", reason="seed")
money.mint("u:21", 500, service="owner", reason="seed")
age_wallet("u:20")
age_wallet("u:21")
mkt2 = predictions.open_market("Coin toss", ["heads", "tails"], created_by="owner")
predictions.stake(mkt2, "u:20", "heads", 200)
predictions.stake(mkt2, "u:21", "heads", 150)
predictions.close(mkt2)
res3 = predictions.resolve(mkt2, "tails", money.new_event_id("pred.resolve"))
check("no winners means paid_out is 0", res3["paid_out"] == 0)
check("the whole pool lands with the house", res3["remainder"] + res3["rake"] == res3["pool"])
check("both stakers actually lost their stakes",
      money.balance("u:20").coins == 300 and money.balance("u:21").coins == 350)

# ------------------------------------------------------------------ void refunds exactly
print("\nvoiding returns every stake exactly")
reset()
money.mint("u:30", 700, service="owner", reason="seed")
money.mint("u:31", 900, service="owner", reason="seed")
age_wallet("u:30")
age_wallet("u:31")
mkt3 = predictions.open_market("Will it rain?", ["yes", "no"], created_by="owner")
predictions.stake(mkt3, "u:30", "yes", 250)
predictions.stake(mkt3, "u:31", "no", 300)
released = predictions.void(mkt3)
check("void released both stakes", released == 2)
check("u:30 got their exact stake back", money.balance("u:30").coins == 700)
check("u:31 got their exact stake back", money.balance("u:31").coins == 900)
check("nothing left held after voiding", money.balance("u:30").held == 0 and money.balance("u:31").held == 0)

# re-voiding an already-voided market is a harmless no-op
released_again = predictions.void(mkt3)
check("re-voiding an already-voided market releases nothing further", released_again == 0)

mkt3b_resolved_check = predictions.open_market("Already resolved", ["x", "y"], created_by="owner")
predictions.stake(mkt3b_resolved_check, "u:30", "x", 50)
predictions.close(mkt3b_resolved_check)
predictions.resolve(mkt3b_resolved_check, "x", money.new_event_id("pred.resolve"))
raises("voiding an already-resolved market is refused", predictions.AlreadyResolved,
       predictions.void, mkt3b_resolved_check)

# ------------------------------------------------------------------ gambling_blocked / limits apply to stakes too
print("\nprediction stakes respect the same money-layer guardrails")
reset()
money.mint("u:40", 1000, service="owner", reason="seed")
age_wallet("u:40")
money.set_flag("u:40", "gambling_blocked", service="owner", set_by="owner")
mkt4 = predictions.open_market("Blocked test", ["a", "b"], created_by="owner")
raises("a self-excluded wallet cannot stake", money.GamblingBlocked,
       predictions.stake, mkt4, "u:40", "a", 50)

raises("cannot stake on an unknown outcome", predictions.UnknownOutcome,
       predictions.stake, mkt4, "u:40", "not-a-real-outcome", 50)

# ------------------------------------------------------------------ pool conservation, randomised
print("\npari-mutuel pool conservation over randomised stake distributions")
random.seed(20260826)
conservation_failures = 0
for trial in range(200):
    reset()
    n_players = random.randint(2, 12)
    rake_bps = random.choice([0, 0, 0, 100, 500, 1000])
    subjects = [f"u:{100 + i}" for i in range(n_players)]
    for s in subjects:
        money.mint(s, 100_000, service="owner", reason="seed")
        age_wallet(s)

    mkt_r = predictions.open_market("random market", ["A", "B"], created_by="owner",
                                     rake_bps=rake_bps)
    outcomes_chosen = []
    for s in subjects:
        outcome = random.choice(["A", "B"])
        amount = random.randint(1, 5000)
        predictions.stake(mkt_r, s, outcome, amount)
        outcomes_chosen.append(outcome)

    winner = random.choice(["A", "B"])
    predictions.close(mkt_r)
    r = predictions.resolve(mkt_r, winner, money.new_event_id("pred.resolve"))
    if r["paid_out"] + r["rake"] + r["remainder"] != r["pool"]:
        conservation_failures += 1
    if r["remainder"] < 0 or r["paid_out"] < 0:
        conservation_failures += 1

    with db.db() as c:
        total_ledger = c.execute(
            "SELECT COALESCE(SUM(delta), 0) AS t FROM ledger_entries"
        ).fetchone()["t"]
        total_coins = c.execute(
            "SELECT COALESCE(SUM(coins), 0) AS t FROM wallets"
        ).fetchone()["t"]
    if total_ledger != total_coins:
        conservation_failures += 1

check("pool conservation holds across 200 randomised markets", conservation_failures == 0,
      f"{conservation_failures} failures")

# ------------------------------------------------------------------ treasury:games never mints, end to end
print("\ntreasury:games balance only ever moves by transfer/capture, never mint")
reset()
money.mint("u:50", 1000, service="owner", reason="seed")
with db.db() as c:
    c.execute("UPDATE wallets SET created_at = datetime('now', '-30 days') WHERE subject = 'u:50'")
money.mint("u:50", 100_000, service="owner", reason="topup for repeated play")
for i in range(20):
    games.play("u:50", "coinflip", "heads", 10, client_seed=f"seed-{i}", nonce=i)
with db.db() as c:
    ledger_svc = c.execute(
        "SELECT DISTINCT service FROM ledger_entries WHERE subject = 'treasury:games'"
    ).fetchall()
check("every treasury:games ledger entry came from a scoped service move, not a mint",
      all(r["service"] == "games" for r in ledger_svc))

# ------------------------------------------------------------------ MAX_DAILY_LOSS is WALLET-WIDE
# CONTRACT.md section 9 lists MAX_DAILY_LOSS as ONE guardrail enforced
# server-side at hold time. If exposure is bucketed per wager kind, a player
# runs casino and prediction stakes side by side and realises ~2x the cap
# while every individual wager passes the check. This proves the COMBINED
# accepted exposure never crosses MAX_DAILY_LOSS.
print("\nMAX_DAILY_LOSS is one wallet-wide cap across both wager kinds")
reset()
money.mint("u:60", 1_000_000, service="owner", reason="seed")
age_wallet("u:60")

mkt_cap = predictions.open_market("cap test", ["a", "b"], created_by="owner")
rid_cap = games.open_round("coinflip", "cap-seed", 0)

STAKE = games.MAX_BET                       # 5,000 -- 4 of these is exactly the cap
accepted = 0
refusals: list[str] = []
for i in range(12):                          # far more attempts than the cap allows
    if i % 2 == 0:
        try:
            games.place_bet(rid_cap, "u:60", "heads", STAKE)
            accepted += STAKE
        except games.DailyLossExceeded:
            refusals.append("games")
    else:
        try:
            predictions.stake(mkt_cap, "u:60", "a", STAKE)
            accepted += STAKE
        except predictions.WagerRefused:
            refusals.append("predictions")

check("combined casino + prediction exposure never exceeds MAX_DAILY_LOSS",
      accepted <= games.MAX_DAILY_LOSS,
      f"accepted {accepted:,} against a cap of {games.MAX_DAILY_LOSS:,}")
check("the cap is actually reachable (not refusing everything)",
      accepted == games.MAX_DAILY_LOSS, f"accepted {accepted:,}")
check("both kinds refuse once the shared cap is full",
      "games" in refusals and "predictions" in refusals, f"refusals={refusals}")

with db.db() as c:
    held = c.execute(
        "SELECT COALESCE(SUM(amount - captured - released), 0) AS h FROM ledger_holds "
        "WHERE subject = 'u:60' AND state = 'open'"
    ).fetchone()["h"]
check("open holds match the accepted total exactly", held == accepted, f"held={held}")

# The first wager of the OTHER kind, once one kind has filled the cap on its
# own, must be refused -- that is the exact bypass.
reset()
money.mint("u:61", 1_000_000, service="owner", reason="seed")
age_wallet("u:61")
rid_cap2 = games.open_round("dice", "cap-seed-2", 0)
for _ in range(4):
    games.place_bet(rid_cap2, "u:61", "1", games.MAX_BET)        # 20,000: the whole cap
check("casino alone can fill the cap", money.balance("u:61").held == games.MAX_DAILY_LOSS,
      f"held={money.balance('u:61').held}")
mkt_cap2 = predictions.open_market("cap test 2", ["a", "b"], created_by="owner")
raises("a prediction stake on top of a full casino cap is refused",
       predictions.WagerRefused, predictions.stake, mkt_cap2, "u:61", "a", 1)
check("the refused stake left no hold behind",
      money.balance("u:61").held == games.MAX_DAILY_LOSS)

# and the mirror image: predictions fill the cap, casino is refused
reset()
money.mint("u:62", 1_000_000, service="owner", reason="seed")
age_wallet("u:62")
mkt_cap3 = predictions.open_market("cap test 3", ["a", "b"], created_by="owner")
for _ in range(4):
    predictions.stake(mkt_cap3, "u:62", "a", games.MAX_BET)
check("predictions alone can fill the cap",
      money.balance("u:62").held == games.MAX_DAILY_LOSS,
      f"held={money.balance('u:62').held}")
rid_cap3 = games.open_round("coinflip", "cap-seed-3", 0)
raises("a casino bet on top of a full prediction cap is refused",
       games.DailyLossExceeded, games.place_bet, rid_cap3, "u:62", "heads", 1)
check("the refused bet left no hold behind",
      money.balance("u:62").held == games.MAX_DAILY_LOSS)

# realised loss and open exposure share the same budget
reset()
money.mint("u:63", 1_000_000, service="owner", reason="seed")
age_wallet("u:63")
with db.db() as c:
    c.execute(
        "INSERT INTO gambling_day (subject, day, staked, lost) "
        "VALUES ('u:63', strftime('%Y-%m-%d','now'), 0, ?)",
        (games.MAX_DAILY_LOSS - 5_000,),
    )
mkt_cap4 = predictions.open_market("cap test 4", ["a", "b"], created_by="owner")
predictions.stake(mkt_cap4, "u:63", "a", 5_000)                  # exactly the room left
rid_cap4 = games.open_round("dice", "cap-seed-4", 0)
raises("realised loss + open prediction exposure closes the casino door",
       games.DailyLossExceeded, games.place_bet, rid_cap4, "u:63", "1", 1)


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all betting tests pass")
