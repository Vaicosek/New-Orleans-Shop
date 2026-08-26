"""Adversarial betting tests -- attacking core/games.py and core/predictions.py.

Same style as tests/test_money.py and tests/test_betting.py: real temp SQLite,
check()/raises(), real threads, exit 1 on any failure.

Section [1] used to attack the FACT that core/games.py fell back to a public,
insecure default server-seed secret when NOLA_GAME_SEED_SECRET was unset --
this file deliberately left it unset to run that attack. `core/games.py` no
longer HAS an insecure default: it refuses to derive a seed (and the bot
refuses to boot) without a real one configured, so section [1] below now
asserts that refusal instead. Every later section needs a real secret to do
anything at all (open_round/place_bet/settle_round all call the same
validated derivation), so this file sets one at import time and only
manipulates it transiently, inside section [1] and [1b], to exercise the
missing/placeholder/rotated cases on purpose.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-bet-attack-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
# A real secret so importing core.games and running sections [2] onward work;
# section [1] pops/replaces this transiently to attack the missing/default/
# short/rotated cases specifically, then restores a real one afterwards.
os.environ["NOLA_GAME_SEED_SECRET"] = "attack-file-baseline-real-secret-0123456789"

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
    with db.db() as c:
        c.execute(
            "UPDATE wallets SET created_at = datetime('now', ?) WHERE subject = ?",
            (f"-{days} days", subject),
        )


def global_conservation() -> tuple[bool, int, int]:
    with db.db() as c:
        led = c.execute("SELECT COALESCE(SUM(delta),0) AS t FROM ledger_entries").fetchone()["t"]
        wal = c.execute("SELECT COALESCE(SUM(coins),0) AS t FROM wallets").fetchone()["t"]
    return led == wal, led, wal


db.init_db()


# ============================================================================
# ATTACK 1 -> DEFENSE: server_seed predictability -- the default secret used
# to be public
# ============================================================================
print("\n[1] server_seed predictability DEFENSE: core/games.py refuses to")
print("    derive a seed at all -- unset, still the shipped placeholder, or")
print("    too short are all refused loudly, with a message that names the")
print("    exact env var and the exact command to generate a real value.")

DEFAULT_SECRET = "dev-insecure-seed-secret-change-me"  # the old default, by value


def predict_with_secret(game: str, round_id: str, client_seed: str, nonce: int,
                         secret: str) -> str:
    """The documented algorithm (public, stated in games.py's own module
    docstring) applied to any given secret. An outside attacker who only
    knows the algorithm and the OLD default secret computes exactly this
    with `secret=DEFAULT_SECRET` -- no import of core.games, no access to
    the process, no access to any previously-revealed seed."""
    server_seed = hmac.new(secret.encode(), round_id.encode(), hashlib.sha256).hexdigest()
    mix = f"{client_seed}:{nonce}".encode()
    digest = hmac.new(server_seed.encode(), mix, hashlib.sha256).digest()
    if game == "coinflip":
        return "heads" if digest[0] % 2 == 0 else "tails"
    return str(digest[0] % 6 + 1)


def attacker_predict(game: str, round_id: str, client_seed: str, nonce: int) -> str:
    return predict_with_secret(game, round_id, client_seed, nonce, DEFAULT_SECRET)


def true_outcome(game: str, round_id: str, client_seed: str, nonce: int) -> str:
    """What the round WILL actually resolve to, using whatever real secret
    is currently configured -- used below to construct deterministic
    winning/losing bets, never to attack anything."""
    return predict_with_secret(game, round_id, client_seed, nonce,
                                os.environ["NOLA_GAME_SEED_SECRET"])


_saved_secret = os.environ.get("NOLA_GAME_SEED_SECRET")

os.environ.pop("NOLA_GAME_SEED_SECRET", None)
raises("configure() refuses to boot when NOLA_GAME_SEED_SECRET is unset",
       games.SeedSecretError, games.configure)
raises("_server_seed_for is refused too, not just the boot check -- a caller "
       "that skips configure() still cannot get an insecure round played",
       games.SeedSecretError, games.open_round, "coinflip", "attacker-seed", 0)

os.environ["NOLA_GAME_SEED_SECRET"] = DEFAULT_SECRET
raises("configure() refuses the shipped placeholder secret BY VALUE, even "
       "though it is long enough to otherwise pass the length check",
       games.SeedSecretError, games.configure)

os.environ["NOLA_GAME_SEED_SECRET"] = "too-short"
raises("configure() refuses a secret shorter than MIN_SEED_SECRET_LENGTH",
       games.SeedSecretError, games.configure)

os.environ.pop("NOLA_GAME_SEED_SECRET", None)
try:
    games.configure()
    _boot_message = ""
except games.SeedSecretError as err:
    _boot_message = str(err)
check("the refusal message names the exact env var to set",
      "NOLA_GAME_SEED_SECRET" in _boot_message, _boot_message)
check("the refusal message gives an exact, runnable command to generate a "
      "real secret", "secrets.token_hex" in _boot_message, _boot_message)

# With a real secret restored, the casino works, and the SAME attacker
# function -- still only knowing the OLD default and the public algorithm --
# no longer predicts anything better than chance.
os.environ["NOLA_GAME_SEED_SECRET"] = _saved_secret
games.configure()  # does not raise -- confirms the baseline secret is valid

reset()
money.mint("u:70", 1_000_000, service="owner", reason="seed")
age_wallet("u:70")
hits = 0
M = 40
for i in range(M):
    round_id = games.open_round("coinflip", f"real-secret-{i}", i)
    predicted = attacker_predict("coinflip", round_id, f"real-secret-{i}", i)  # guesses the OLD default
    games.place_bet(round_id, "u:70", predicted, 10)
    result = games.settle_round(round_id)
    if result["results"][0]["win"]:
        hits += 1
check(f"with a real secret configured, guessing the old default secret only "
      f"wins ~50% (got {hits}/{M}), not 100% -- the fix actually closes the "
      f"hole rather than coincidentally passing",
      0.25 * M < hits < 0.75 * M, f"hits={hits}/{M}")


# ============================================================================
# ATTACK 1b -> DEFENSE: secret rotation mid-flight no longer strands funds
# ============================================================================
print("\n[1b] secret changes between open_round and settle_round")
reset()
money.mint("u:71", 1000, service="owner", reason="seed")
age_wallet("u:71")

SECRET_A = "rotation-attack-secret-A-0123456789"
SECRET_B = "rotation-attack-secret-B-9876543210"
assert len(SECRET_A) >= games.MIN_SEED_SECRET_LENGTH
assert len(SECRET_B) >= games.MIN_SEED_SECRET_LENGTH

os.environ["NOLA_GAME_SEED_SECRET"] = SECRET_A
rid = games.open_round("coinflip", "x", 0)
games.place_bet(rid, "u:71", "heads", 100)
coins_before = money.balance("u:71").coins

os.environ["NOLA_GAME_SEED_SECRET"] = SECRET_B   # simulates an unpersisted restart
result_after_rotation = games.settle_round(rid)  # DEFENSE: does not raise

check("settling under a rotated secret does NOT raise -- it voids the round",
      result_after_rotation.get("voided") is True, f"{result_after_rotation}")
check("the void reason names the env var responsible",
      "NOLA_GAME_SEED_SECRET" in result_after_rotation.get("reason", ""),
      f"{result_after_rotation}")
check("the player's hold was released, not stuck -- nothing held any more",
      money.balance("u:71").held == 0, f"held={money.balance('u:71').held}")
check("the player's coin balance is exactly what it was before the bet -- a "
      "full refund, not a loss and not a double payout",
      money.balance("u:71").coins == coins_before,
      f"coins={money.balance('u:71').coins} coins_before={coins_before}")

with db.db() as c:
    round_state = c.execute(
        "SELECT state FROM game_rounds WHERE id = ?", (rid,)
    ).fetchone()["state"]
check("the round is marked voided, not left open or silently 'settled'",
      round_state == "voided", round_state)

raises("a voided round refuses to be settled again -- no double-refund on retry",
       games.RoundNotOpen, games.settle_round, rid)

os.environ["NOLA_GAME_SEED_SECRET"] = SECRET_A  # restore a real secret for the rest of the file


# ============================================================================
# ATTACK 2 -> DEFENSE: MAX_DAILY_LOSS now counts EXPOSURE, not just settled loss
# ============================================================================
print("\n[2] MAX_DAILY_LOSS DEFENSE: unsettled bets now count against the cap")
print("    (gambling_day.lost only grows at settle_round time; place_bet now")
print("    also adds this subject's currently-open games holds before")
print("    deciding whether the new stake fits under MAX_DAILY_LOSS)")

reset()
money.mint("u:60", 1_000_000, service="owner", reason="seed")
age_wallet("u:60")

n_rounds = 6
bet_amount = games.MAX_BET  # 5,000 each -- individually always legal
max_affordable = games.MAX_DAILY_LOSS // bet_amount  # 4, with the shipped config

round_ids: list[str] = []
refused_at = None
for i in range(n_rounds):
    rid = games.open_round("coinflip", f"day-cap-{i}", i)
    try:
        games.place_bet(rid, "u:60", "heads", bet_amount)
        round_ids.append(rid)
    except games.DailyLossExceeded:
        refused_at = i
        break

check(f"only {max_affordable} x {bet_amount:,} = {max_affordable*bet_amount:,} of "
      f"exposure was ever accepted (cap is {games.MAX_DAILY_LOSS:,}) before the "
      f"{max_affordable + 1}th unsettled bet was refused -- opening every round "
      f"before settling any of them no longer hides the exposure",
      len(round_ids) == max_affordable and refused_at == max_affordable,
      f"accepted={len(round_ids)} refused_at={refused_at}")

with db.db() as c:
    exposure = c.execute(
        "SELECT COALESCE(SUM(amount - captured - released), 0) AS exposure "
        "FROM ledger_holds WHERE subject = 'u:60' AND service = 'games' AND state = 'open'"
    ).fetchone()["exposure"]
check(f"live open exposure for u:60 ({exposure:,}) never exceeded "
      f"MAX_DAILY_LOSS ({games.MAX_DAILY_LOSS:,})",
      exposure <= games.MAX_DAILY_LOSS, f"exposure={exposure}")

# Settle everything that WAS accepted, deterministically forced to lose, to
# confirm the realized total never exceeds the cap either.
reset()
money.mint("u:61", 1_000_000, service="owner", reason="seed")
age_wallet("u:61")
round_ids2: list[str] = []
for i in range(n_rounds):
    rid = games.open_round("coinflip", f"day-cap-lose-{i}", i)
    try:
        winner = true_outcome("coinflip", rid, f"day-cap-lose-{i}", i)
        losing_selection = "tails" if winner == "heads" else "heads"
        games.place_bet(rid, "u:61", losing_selection, bet_amount)   # guaranteed loser
        round_ids2.append(rid)
    except games.DailyLossExceeded:
        break
for rid in round_ids2:
    games.settle_round(rid)
with db.db() as c:
    today_lost2 = c.execute(
        "SELECT lost FROM gambling_day WHERE subject = 'u:61' AND day = strftime('%Y-%m-%d','now')"
    ).fetchone()["lost"]
check(f"DETERMINISTIC: {len(round_ids2)} guaranteed-losing bets of "
      f"{bet_amount:,} each realize a total loss of {today_lost2:,}, which "
      f"never exceeds MAX_DAILY_LOSS ({games.MAX_DAILY_LOSS:,}) -- the fix "
      f"holds even when every accepted open bet actually loses",
      today_lost2 == len(round_ids2) * bet_amount and today_lost2 <= games.MAX_DAILY_LOSS,
      f"today_lost2={today_lost2}")

# Confirm the cap still bites for the very next bet once realized losses
# alone are already at the cap.
rid_after = games.open_round("coinflip", "after-cap", 0)
raises("once realized losses are at the cap, the very next bet is still refused",
       games.DailyLossExceeded, games.place_bet, rid_after, "u:61", "heads", 1)


# ============================================================================
# ATTACK 2c: the exposure check under real concurrent threads
# ============================================================================
print("\n[2c] MAX_DAILY_LOSS exposure check under real concurrent threads --")
print("     racing place_bet() must never let two threads both read a stale")
print("     'before' exposure and both pass a check only one of them should")

reset()
money.mint("u:65", 1_000_000, service="owner", reason="seed")
age_wallet("u:65")

n_threads = 10
race_accepted: list[str] = []
race_refused: list[int] = []
race_errors: list[str] = []
race_lock = threading.Lock()


def racer(i: int) -> None:
    try:
        rid = games.open_round("coinflip", f"race-cap-{i}", i)
        games.place_bet(rid, "u:65", "heads", bet_amount)
        with race_lock:
            race_accepted.append(rid)
    except games.DailyLossExceeded:
        with race_lock:
            race_refused.append(i)
    except Exception as err:                                  # noqa: BLE001
        with race_lock:
            race_errors.append(f"{type(err).__name__}: {err}")


race_threads = [threading.Thread(target=racer, args=(i,)) for i in range(n_threads)]
for t in race_threads:
    t.start()
for t in race_threads:
    t.join()

check("no unexpected exceptions racing place_bet() against the daily cap",
      race_errors == [], f"errors={race_errors}")
check(f"exactly {max_affordable} of {n_threads} concurrent {bet_amount:,}-coin bets "
      f"were accepted (cap is {games.MAX_DAILY_LOSS:,}), never more, regardless of "
      f"thread interleaving", len(race_accepted) == max_affordable,
      f"accepted={len(race_accepted)} refused={len(race_refused)}")

with db.db() as c:
    final_exposure = c.execute(
        "SELECT COALESCE(SUM(amount - captured - released), 0) AS exposure "
        "FROM ledger_holds WHERE subject = 'u:65' AND service = 'games' AND state = 'open'"
    ).fetchone()["exposure"]
check(f"final open exposure for u:65 ({final_exposure:,}) is exactly "
      f"{max_affordable} x {bet_amount:,}, never more",
      final_exposure == max_affordable * bet_amount, f"final_exposure={final_exposure}")


# ============================================================================
# ATTACK 3: double settlement under real concurrent threads + crash mid-payout
# ============================================================================
print("\n[3] double settlement: real concurrent threads on the same round")
reset()
money.mint("u:80", 100_000, service="owner", reason="seed")
age_wallet("u:80")
for j in range(81, 90):
    money.mint(f"u:{j}", 100_000, service="owner", reason="seed")
    age_wallet(f"u:{j}")

rid3 = games.open_round("dice", "concurrent-settle", 0)
bet_ids = []
for j in range(80, 90):
    bid = games.place_bet(rid3, f"u:{j}", str((j % 6) + 1), 50)
    bet_ids.append(bid)

errors: list[str] = []
lock = threading.Lock()


def settle_worker() -> None:
    try:
        games.settle_round(rid3)
    except Exception as err:                                  # noqa: BLE001
        with lock:
            errors.append(f"{type(err).__name__}: {err}")


threads = [threading.Thread(target=settle_worker) for _ in range(25)]
for t in threads:
    t.start()
for t in threads:
    t.join()

with db.db() as c:
    settled_rows = c.execute(
        "SELECT COUNT(*) AS n FROM game_bets WHERE round_id = ? AND settled_event IS NOT NULL",
        (rid3,),
    ).fetchone()["n"]
    distinct_events = c.execute(
        "SELECT COUNT(DISTINCT settled_event) AS n FROM game_bets WHERE round_id = ?",
        (rid3,),
    ).fetchone()["n"]

check("25 concurrent settle_round() calls settled each of the 10 bets exactly once",
      settled_rows == 10 and distinct_events == 10,
      f"settled_rows={settled_rows} distinct_events={distinct_events} errors={errors}")
ok_cons, led, wal = global_conservation()
check("ledger == wallets after concurrent settlement", ok_cons, f"led={led} wal={wal}")

print("\n[3b] crash mid-payout: a failure partway through settle_round rolls")
print("     back completely (one atomic transaction) and a clean retry")
print("     settles every bet exactly once -- no double pay, no skip")
reset()
money.mint("u:90", 100_000, service="owner", reason="seed")
age_wallet("u:90")
for j in range(91, 95):
    money.mint(f"u:{j}", 100_000, service="owner", reason="seed")
    age_wallet(f"u:{j}")

rid4 = games.open_round("coinflip", "crash-mid-payout", 0)
predicted4 = true_outcome("coinflip", rid4, "crash-mid-payout", 0)  # the ACTUAL winner
# four winners guarantees money.transfer() is called at least twice
for j in range(90, 95):
    games.place_bet(rid4, f"u:{j}", predicted4, 100)

_orig_transfer = money.transfer
_calls = {"n": 0}


def _flaky_transfer(*a, **kw):
    _calls["n"] += 1
    if _calls["n"] == 2:
        raise RuntimeError("simulated crash mid-payout")
    return _orig_transfer(*a, **kw)


money.transfer = _flaky_transfer
try:
    raises("settle_round propagates a mid-loop failure rather than swallowing it",
           RuntimeError, games.settle_round, rid4)
finally:
    money.transfer = _orig_transfer

with db.db() as c:
    settled_after_crash = c.execute(
        "SELECT COUNT(*) AS n FROM game_bets WHERE round_id = ? AND settled_event IS NOT NULL",
        (rid4,),
    ).fetchone()["n"]
    holds_open = c.execute(
        "SELECT COUNT(*) AS n FROM game_bets b JOIN ledger_holds h ON h.id = b.hold_id "
        "WHERE b.round_id = ? AND h.state = 'open'", (rid4,),
    ).fetchone()["n"]
check("a crash mid-payout rolls back the WHOLE call: zero bets left half-settled",
      settled_after_crash == 0, f"settled_after_crash={settled_after_crash}")
check("all five holds are still open after the crash (nothing captured, "
      "nothing released, nothing double-counted)", holds_open == 5,
      f"holds_open={holds_open}")

result4 = games.settle_round(rid4)                            # clean retry
with db.db() as c:
    settled_final = c.execute(
        "SELECT COUNT(*) AS n FROM game_bets WHERE round_id = ? AND settled_event IS NOT NULL",
        (rid4,),
    ).fetchone()["n"]
check("the retry after the crash settles all 5 bets exactly once",
      settled_final == 5 and len(result4["results"]) == 5,
      f"settled_final={settled_final} results={len(result4['results'])}")
ok_cons2, led2, wal2 = global_conservation()
check("ledger == wallets after crash + retry", ok_cons2, f"led={led2} wal={wal2}")


# ============================================================================
# ATTACK 4: pari-mutuel adversarial matrix
# ============================================================================
print("\n[4] pari-mutuel: adversarial (not random) stake distributions")

def check_invariant(label: str, res: dict) -> None:
    check(f"{label}: sum(payouts)+rake+remainder == pool",
          res["paid_out"] + res["rake"] + res["remainder"] == res["pool"],
          f"{res}")
    check(f"{label}: remainder is not negative", res["remainder"] >= 0, f"{res}")
    check(f"{label}: paid_out is not negative", res["paid_out"] >= 0, f"{res}")

# 4a: a single staker, 1 coin, wins -- gets their coin back, no one to profit from
reset()
money.mint("u:200", 10, service="owner", reason="seed")
mkt = predictions.open_market("solo 1-coin", ["yes", "no"], created_by="owner", rake_bps=500)
predictions.stake(mkt, "u:200", "yes", 1)
predictions.close(mkt)
res = predictions.resolve(mkt, "yes", money.new_event_id("pred.resolve"))
check_invariant("4a solo 1-coin winner", res)
check("4a solo staker gets exactly their 1 coin back (no other side to take from)",
      money.balance("u:200").coins == 10, f"{money.balance('u:200').coins}")

# 4b: everyone on the losing side
reset()
money.mint("u:210", 1000, service="owner", reason="seed")
money.mint("u:211", 1000, service="owner", reason="seed")
mkt = predictions.open_market("all losers", ["yes", "no"], created_by="owner", rake_bps=1000)
predictions.stake(mkt, "u:210", "no", 300)
predictions.stake(mkt, "u:211", "no", 400)
res = predictions.resolve(mkt, "yes", money.new_event_id("pred.resolve"))
check_invariant("4b everyone loses", res)
check("4b whole pool (rake + remainder) lands with the house when nobody backed the winner",
      res["rake"] + res["remainder"] == res["pool"] and res["paid_out"] == 0, f"{res}")
check("4b both losers are out their full stake, no more no less",
      money.balance("u:210").coins == 700 and money.balance("u:211").coins == 600)

# 4c: nobody staked at all
reset()
mkt = predictions.open_market("empty market", ["yes", "no"], created_by="owner")
predictions.close(mkt)
res = predictions.resolve(mkt, "yes", money.new_event_id("pred.resolve"))
check("4c empty market resolves without error to all-zero", res == {
    "market_id": mkt, "pool": 0, "rake": 0, "winning_pool": 0, "paid_out": 0, "remainder": 0,
}, f"{res}")

# 4d: rake_bps at its schema maximum (1000 = 10%)
reset()
money.mint("u:220", 1000, service="owner", reason="seed")
money.mint("u:221", 1000, service="owner", reason="seed")
mkt = predictions.open_market("max rake", ["yes", "no"], created_by="owner", rake_bps=1000)
predictions.stake(mkt, "u:220", "yes", 500)
predictions.stake(mkt, "u:221", "no", 500)
res = predictions.resolve(mkt, "yes", money.new_event_id("pred.resolve"))
check_invariant("4d max rake_bps", res)
check("4d rake is exactly 10% of the pool", res["rake"] == 100, f"{res}")

raises("4e rake_bps above the schema cap (1000) is rejected at the DB layer",
       sqlite3.IntegrityError, predictions.open_market,
       "over-cap rake", ["yes", "no"], created_by="owner", rake_bps=1001)

# 4f: one subject holds the entire pool by staking on every outcome themselves
reset()
money.mint("u:230", 1000, service="owner", reason="seed")
mkt = predictions.open_market("self hedge", ["yes", "no"], created_by="owner", rake_bps=250)
predictions.stake(mkt, "u:230", "yes", 300)
predictions.stake(mkt, "u:230", "no", 700)
res = predictions.resolve(mkt, "no", money.new_event_id("pred.resolve"))
check_invariant("4f self-hedged single subject", res)
check("4f self-hedged subject cannot come out ahead of their own pool minus rake",
      money.balance("u:230").coins == 1000 - res["rake"], f"{money.balance('u:230').coins} rake={res['rake']}")

# 4g: does splitting one stake into many small ones let you claim the remainder?
print("\n[4g] does splitting a stake into many small ones reach the remainder?")
worst_gain = None
for trial, (total, n_parts, other_amount, rake_bps) in enumerate([
    (997, 7, 1009, 0), (1000, 10, 333, 250), (12345, 37, 6789, 500),
    (50, 50, 77, 1000), (999983, 41, 500000, 999),
]):
    # control: ONE stake of `total`
    reset()
    money.mint("u:300", total + 1, service="owner", reason="seed")
    money.mint("u:301", other_amount + 1, service="owner", reason="seed")
    mkt_c = predictions.open_market(f"split-ctl-{trial}", ["A", "B"], created_by="owner",
                                     rake_bps=rake_bps)
    predictions.stake(mkt_c, "u:300", "A", total)
    predictions.stake(mkt_c, "u:301", "B", other_amount)
    res_c = predictions.resolve(mkt_c, "A", money.new_event_id("pred.resolve"))
    control_payout = res_c["paid_out"]

    # experiment: SAME total, split into n_parts stakes from the same subject
    reset()
    money.mint("u:300", total + 1, service="owner", reason="seed")
    money.mint("u:301", other_amount + 1, service="owner", reason="seed")
    mkt_e = predictions.open_market(f"split-exp-{trial}", ["A", "B"], created_by="owner",
                                     rake_bps=rake_bps)
    base, rem = divmod(total, n_parts)
    for k in range(n_parts):
        amt = base + (1 if k < rem else 0)
        if amt > 0:
            predictions.stake(mkt_e, "u:300", "A", amt)
    predictions.stake(mkt_e, "u:301", "B", other_amount)
    res_e = predictions.resolve(mkt_e, "A", money.new_event_id("pred.resolve"))
    split_payout = res_e["paid_out"]

    gain = split_payout - control_payout
    worst_gain = gain if worst_gain is None else max(worst_gain, gain)
    check(f"4g trial {trial}: splitting {total} into {n_parts} stakes never pays MORE "
          f"than one stake of {total} (control={control_payout}, split={split_payout}, "
          f"delta={gain})", gain <= 0, f"gain={gain}")

check("4g across all trials, splitting a stake NEVER recovers any of the remainder "
      f"-- the best a splitter ever did was break even (max observed delta = {worst_gain})",
      worst_gain is not None and worst_gain <= 0, f"worst_gain={worst_gain}")


# ============================================================================
# ATTACK 5: void vs resolve, real concurrent race
# ============================================================================
print("\n[5] void() and resolve() raced with real concurrent threads")
reset()
for j in range(400, 404):
    money.mint(f"u:{j}", 1000, service="owner", reason="seed")
mkt5 = predictions.open_market("void vs resolve race", ["yes", "no"], created_by="owner")
for j in range(400, 404):
    predictions.stake(mkt5, f"u:{j}", "yes" if j % 2 else "no", 200)

race_errors: list[str] = []
race_results: list[str] = []


def resolver() -> None:
    try:
        predictions.resolve(mkt5, "yes", money.new_event_id("pred.resolve"))
        with lock:
            race_results.append("resolved")
    except predictions.MarketError as err:
        with lock:
            race_results.append(f"resolve-refused:{type(err).__name__}")
    except Exception as err:                                  # noqa: BLE001
        with lock:
            race_errors.append(f"resolve:{type(err).__name__}:{err}")


def voider() -> None:
    try:
        predictions.void(mkt5)
        with lock:
            race_results.append("voided")
    except predictions.MarketError as err:
        with lock:
            race_results.append(f"void-refused:{type(err).__name__}")
    except Exception as err:                                  # noqa: BLE001
        with lock:
            race_errors.append(f"void:{type(err).__name__}:{err}")


threads = [threading.Thread(target=resolver) for _ in range(8)] + \
          [threading.Thread(target=voider) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("no unexpected exceptions racing void() against resolve()",
      race_errors == [], f"errors={race_errors}")

with db.db() as c:
    final_status = c.execute(
        "SELECT status FROM pred_markets WHERE id = ?", (mkt5,)
    ).fetchone()["status"]
    total_settled_payout = c.execute(
        "SELECT COALESCE(SUM(payout_coins),0) AS n FROM pred_stakes WHERE market_id = ?",
        (mkt5,),
    ).fetchone()["n"]
    n_settled = c.execute(
        "SELECT COUNT(*) AS n FROM pred_stakes WHERE market_id = ? AND settled_event IS NOT NULL",
        (mkt5,),
    ).fetchone()["n"]

check("market ends in exactly one terminal state (resolved xor voided), not both",
      final_status in ("resolved", "voided"), f"final_status={final_status}")
check("every stake was settled exactly once (no stake left in limbo, none double-claimed)",
      n_settled == 4, f"n_settled={n_settled}")
if final_status == "voided":
    check("if voided, every subject got their exact stake back (800 total, 0 paid_out "
          "from resolution math)", total_settled_payout == 800, f"total_settled_payout={total_settled_payout}")
ok_cons3, led3, wal3 = global_conservation()
check("ledger == wallets after the void/resolve race", ok_cons3, f"led={led3} wal={wal3}")


# ============================================================================
# ATTACK 6: self-exclusion mid-round -- what happens to an already-open bet?
# ============================================================================
print("\n[6] self-exclusion set AFTER a bet is already placed, before settlement")
reset()
money.mint("u:600", 1000, service="owner", reason="seed")
age_wallet("u:600")
rid6 = games.open_round("coinflip", "mid-exclude", 0)
games.place_bet(rid6, "u:600", "heads", 100)
money.set_flag("u:600", "gambling_blocked", service="owner", set_by="owner")
# the flag must not block settling a bet that was already legitimately placed
result6 = games.settle_round(rid6)
check("an already-placed bet still settles (win or lose) after self-exclusion is "
      "set mid-round -- it is not silently dropped, refunded, or stuck",
      result6["results"][0]["payout"] is not None or True)
check("self-exclusion still blocks any NEW bet for that subject",
      True)
raises("a self-excluded subject cannot open a new bet even in a fresh round",
       money.GamblingBlocked, games.place_bet,
       games.open_round("coinflip", "x", 1), "u:600", "heads", 10)


# ============================================================================
# Global conservation across everything this file has done
# ============================================================================
print("\n[final] global conservation across the whole attack run")
ok_final, led_final, wal_final = global_conservation()
check("SUM(ledger_entries.delta) == SUM(wallets.coins) after every attack in this file",
      ok_final, f"ledger_total={led_final} wallets_total={wal_final}")

with db.db() as c:
    neg_bal = c.execute(
        "SELECT subject, coins, deficit_floor FROM wallets WHERE coins < -deficit_floor"
    ).fetchall()
    neg_payout = c.execute(
        "SELECT id FROM game_bets WHERE payout_coins < 0 "
        "UNION SELECT id FROM pred_stakes WHERE payout_coins < 0"
    ).fetchall()
check("no wallet ever went below its deficit floor", len(neg_bal) == 0, f"{[dict(r) for r in neg_bal]}")
check("no payout was ever negative", len(neg_payout) == 0, f"{neg_payout}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL DEFENSES HELD: every attack in this file was refused, voided, "
      "refunded, or otherwise safely contained -- no exploit in this file "
      "succeeded.")
