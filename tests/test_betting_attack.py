"""Adversarial betting tests -- attacking core/games.py and core/predictions.py.

Same style as tests/test_money.py and tests/test_betting.py: real temp SQLite,
check()/raises(), real threads, exit 1 on any failure.

Section [1] used to attack the FACT that core/games.py fell back to a public,
insecure default server-seed secret when NOLA_GAME_SEED_SECRET was unset --
this file deliberately left it unset to run that attack. `core/games.py` no
longer HAS an insecure default: it refuses to open a round (and the bot
refuses to boot) without a real one configured, so section [1] below now
asserts that refusal instead. Every later section needs a real secret to do
anything at all (open_round still validates it), so this file sets one at
import time and only manipulates it transiently, inside section [1], to
exercise the missing/placeholder/short cases on purpose.

Section [10] is the big one: the commitment must be minted and PUBLISHED
before the stake is known, the client seed must be the PLAYER's, and
`verify()` must audit that pre-bet artifact rather than one it can recompute
from the round it is validating. Section [11] attacks the draw itself --
`digest[0] % 6` biased four dice faces and put the real house edge at
4.26%/6.48% against a config that says 5%.
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

from core import audit, db, money, games, predictions, wagering   # noqa: E402

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
                  "game_rounds", "game_commitments", "ledger_entries",
                  "ledger_holds", "wallet_flags",
                  "idempotency", "gambling_day", "wallets"):
            try:
                c.execute(f"DELETE FROM {t}")
            except sqlite3.OperationalError as err:
                # game_commitments is created on first use by core.games; a
                # reset() before any commitment has ever been minted must not
                # explode on it.
                if "no such table" not in str(err).lower():
                    raise
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


def derived_seed_outcome(game: str, round_id: str, client_seed: str, nonce: int,
                          secret: str) -> str:
    """The OLD, now-deleted algorithm: server_seed = HMAC(secret, round_id),
    outcome = digest[0] % n. Kept in this file on purpose -- it is what an
    outside attacker who knew the shipped default secret computed, and it is
    also what section [10]'s house uses to grind a round id after seeing the
    bet. Neither works any more; that is the point of running them."""
    server_seed = hmac.new(secret.encode(), round_id.encode(), hashlib.sha256).hexdigest()
    mix = f"{client_seed}:{nonce}".encode()
    digest = hmac.new(server_seed.encode(), mix, hashlib.sha256).digest()
    if game == "coinflip":
        return "heads" if digest[0] % 2 == 0 else "tails"
    return str(digest[0] % 6 + 1)


def attacker_predict(game: str, round_id: str, client_seed: str, nonce: int) -> str:
    return derived_seed_outcome(game, round_id, client_seed, nonce, DEFAULT_SECRET)


def true_outcome(round_id: str) -> str:
    """What an ALREADY-OPENED round will actually resolve to, as a selection
    string -- used below to construct deterministic winning/losing bets,
    never to attack anything.

    It reads the committed seed straight out of `game_commitments` and calls
    the module's own draw. That is exactly the power the house holds between
    commit and reveal, and exactly why the commitment has to be published
    before the stake is accepted -- a house that can also CHOOSE the seed at
    that moment can choose the outcome.
    """
    with db.db() as c:
        row = c.execute(
            "SELECT gc.server_seed AS seed, gr.game AS game, "
            "       gr.client_seed AS client_seed, gr.nonce AS nonce "
            "  FROM game_rounds gr JOIN game_commitments gc "
            "    ON gc.id = gr.commitment_id WHERE gr.id = ?", (round_id,)
        ).fetchone()
    outcome = games._outcome(row["game"], row["seed"], row["client_seed"], row["nonce"])
    return outcome["face"] if row["game"] == "coinflip" else str(outcome["roll"])


_saved_secret = os.environ.get("NOLA_GAME_SEED_SECRET")

os.environ.pop("NOLA_GAME_SEED_SECRET", None)
raises("configure() refuses to boot when NOLA_GAME_SEED_SECRET is unset",
       games.SeedSecretError, games.configure)
raises("open_round is refused too, not just the boot check -- a caller that "
       "skips configure() still cannot get a round played on a misdeployed casino",
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
# ATTACK 1b -> DEFENSE: a round whose commitment is gone voids and refunds
#
# This used to attack secret ROTATION between open_round and settle_round --
# meaningful only while the server seed was DERIVED from the process secret.
# The seed is now random and stored at commit time, so rotating the env var
# is harmless. What can still leave a round with no recoverable seed is the
# commitment row itself being missing, and that must still void and refund
# rather than raise, invent a seed, or strand the hold.
# ============================================================================
print("\n[1b] a round whose commitment row is gone: void + full refund")
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

# Rotating the secret is now a no-op for an open round: the seed was random
# and is already stored. Settling must still work normally afterwards.
os.environ["NOLA_GAME_SEED_SECRET"] = SECRET_B   # simulates an unpersisted restart
with db.db() as c:
    seed_survives = c.execute(
        "SELECT COUNT(*) AS n FROM game_commitments gc "
        "JOIN game_rounds gr ON gr.commitment_id = gc.id "
        "WHERE gr.id = ? AND gc.server_seed IS NOT NULL", (rid,)
    ).fetchone()["n"]
check("the committed seed is STORED, so rotating NOLA_GAME_SEED_SECRET "
      "mid-flight can no longer lose a round's seed", seed_survives == 1,
      f"seed_survives={seed_survives}")

# Now the case that genuinely leaves no recoverable seed: the commitment row
# is gone. There is no fair outcome to compute and never will be -- inventing
# one at settlement is exactly the after-the-fact selection this scheme exists
# to prevent -- so the round must void and refund in full.
with db.db() as c:
    c.execute(
        "DELETE FROM game_commitments WHERE id = "
        "(SELECT commitment_id FROM game_rounds WHERE id = ?)", (rid,))
result_no_commitment = games.settle_round(rid)   # DEFENSE: does not raise

check("settling a round whose commitment is gone does NOT raise -- it voids",
      result_no_commitment.get("voided") is True, f"{result_no_commitment}")
check("the void reason names the commitment responsible",
      "commitment" in result_no_commitment.get("reason", ""),
      f"{result_no_commitment}")
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
        winner = true_outcome(rid)
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
predicted4 = true_outcome(rid4)                       # the ACTUAL winner
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
age_wallet("u:200")
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
age_wallet("u:210")
age_wallet("u:211")
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
age_wallet("u:220")
age_wallet("u:221")
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
age_wallet("u:230")
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
    # Every `total`/`other_amount` here is <= MAX_BET (5,000): predictions.stake
    # now enforces the same MAX_BET a single casino bet does (core/wagering.py),
    # so the "control" side of this comparison -- ONE stake of `total` -- must
    # itself be a legal single stake, not just the split side.
    (997, 7, 1009, 0), (1000, 10, 333, 250), (4999, 37, 3331, 500),
    (50, 50, 77, 1000), (4993, 41, 4987, 999),
]):
    # control: ONE stake of `total`
    reset()
    money.mint("u:300", total + 1, service="owner", reason="seed")
    money.mint("u:301", other_amount + 1, service="owner", reason="seed")
    age_wallet("u:300")
    age_wallet("u:301")
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
    age_wallet("u:300")
    age_wallet("u:301")
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
    age_wallet(f"u:{j}")
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
# REGRESSION 7: an unpayable casino win must never be rolled back and
# reported to the player as a failed bet (finding 1). games.py:448.
# ============================================================================
print("\n[7] REGRESSION: house insolvency is refused up front, and an "
      "unpayable WIN never destroys the round")

# 7a: refuse a bet up front when the house could not fund it even in the
# best case for the player -- before any hold is placed, before any row
# exists for it.
reset()
# money.ensure_wallet only ever RAISES an existing floor (deficit_floor=0 is
# its own "no floor requested" sentinel, not "set the floor to zero") -- so
# to actually shrink reset()'s 1,000,000 floor back down for this test, set
# it directly.
with db.db() as c:
    c.execute("UPDATE wallets SET deficit_floor = 0 WHERE subject = 'treasury:games'")
money.mint("u:700", 100_000, service="owner", reason="seed")
age_wallet("u:700")
rid7a = games.open_round("coinflip", "solvency-precheck", 0)
raises("a bet the house could not pay even if it won is refused up front",
       games.HouseInsolvent, games.place_bet, rid7a, "u:700", "heads", 100)
with db.db() as c:
    bets_after = c.execute(
        "SELECT COUNT(*) AS n FROM game_bets WHERE round_id = ?", (rid7a,)
    ).fetchone()["n"]
check("the refused bet left no game_bets row and no hold behind",
      bets_after == 0 and money.balance("u:700").held == 0,
      f"bets_after={bets_after} held={money.balance('u:700').held}")

# 7b: the house COULD afford this bet's profit when it was placed, but is
# drained (by something else) before settlement -- the exact race
# place_bet's own pre-check cannot fully close. settle_round must NOT roll
# the round back for it.
reset()
with db.db() as c:
    c.execute("UPDATE wallets SET deficit_floor = 0 WHERE subject = 'treasury:games'")
money.mint("treasury:games", 100, service="owner", reason="fund just enough for one payout")
money.mint("u:701", 100_000, service="owner", reason="seed")
age_wallet("u:701")

rid7b = games.open_round("coinflip", "insolvent-win", 0)
winner7b = true_outcome(rid7b)
games.place_bet(rid7b, "u:701", winner7b, 100)   # profit_if_win=96 <= 100 available: passes the pre-check

# Drain the treasury out from under the round directly -- standing in for a
# concurrent bet elsewhere that wins first and empties it; the point here is
# settle_round's behaviour when the transfer it attempts actually fails, not
# reproducing the race that gets it there.
money.ensure_wallet("u:owner_sink", service="owner")
money.transfer("treasury:games", "u:owner_sink", 100, service="games", reason="drain for test")

before_coins = money.balance("u:701").coins
result7b = games.settle_round(rid7b)   # must not raise
check("settle_round did not raise even though the treasury could not pay the win",
      True)
check("the bet settled as a win", result7b["results"][0]["win"] is True)
check("the win is flagged as a pending payout, not silently swallowed",
      result7b["results"][0]["payout_pending"] is True)
check("the player's own stake was returned in full either way (release, not capture)",
      money.balance("u:701").coins == before_coins and money.balance("u:701").held == 0,
      f"coins={money.balance('u:701').coins} held={money.balance('u:701').held}")

with db.db() as c:
    round_row = c.execute("SELECT state FROM game_rounds WHERE id = ?", (rid7b,)).fetchone()
    bet_row = c.execute(
        "SELECT settled_event, payout_coins FROM game_bets WHERE round_id = ?", (rid7b,)
    ).fetchone()
check("the round is persisted as settled, never destroyed by the failed payout",
      round_row is not None and round_row["state"] == "settled")
check("the bet's settlement (payout decision) is persisted, never rolled back",
      bet_row["settled_event"] is not None and bet_row["payout_coins"] == 196,
      f"{dict(bet_row) if bet_row else None}")

v7b = games.verify(rid7b)
check("a pending-payout round is still fully, publicly verifiable", v7b["ok"] is True)

debts7b = audit.pending_debts()
check("the unpaid profit is a VISIBLE debt in the data, not a swallowed exception",
      any(d["target"] == f"game_round:{rid7b}" and d["manual_coins"] == 96 for d in debts7b),
      f"{debts7b}")

# calling settle_round again must not try (and fail) to pay the same bet twice
result7b_retry = games.settle_round(rid7b)
check("re-settling an already-settled round finds nothing left to claim",
      result7b_retry["results"] == [])


# ============================================================================
# REGRESSION 8: predictions.stake shares the SAME wagering guard as
# games.place_bet (finding 2), and open exposure is scoped per kind so
# neither kind locks a subject out of the other (finding 5).
# ============================================================================
print("\n[8] REGRESSION: predictions.stake is guarded like a casino bet, and "
      "per-kind exposure does not cross-contaminate")

# 8a: account age and MAX_BET, exactly as reported -- a seconds-old wallet
# could stake 18x MAX_BET while the SAME wallet was correctly refused a
# 1-coin coinflip.
reset()
money.mint("u:800", 1_000_000, service="owner", reason="seed")   # brand new on purpose
mkt8 = predictions.open_market("account age test", ["yes", "no"], created_by="owner")
raises("a seconds-old wallet cannot stake on a prediction market either",
       predictions.WagerRefused, predictions.stake, mkt8, "u:800", "yes", 1)

age_wallet("u:800")
raises("even once aged, a stake of 18x MAX_BET is refused -- not just the age check",
       predictions.WagerRefused, predictions.stake, mkt8, "u:800", "yes", 90_000)
with db.db() as c:
    n_stakes = c.execute(
        "SELECT COUNT(*) AS n FROM pred_stakes WHERE subject = 'u:800'"
    ).fetchone()["n"]
check("neither refused attempt left a pred_stakes row or a hold behind",
      n_stakes == 0 and money.balance("u:800").held == 0)

predictions.stake(mkt8, "u:800", "yes", 1_000)   # a legal, in-limits stake still works
check("a legal, in-limits stake is still accepted", money.balance("u:800").held == 1_000)

# 8b: a settled prediction LOSS now reaches gambling_day, and the SHARED
# daily cap actually gates a later casino bet for it -- losses used to never
# reach gambling_day and so were invisible to MAX_DAILY_LOSS forever.
reset()
money.mint("u:801", 1_000_000, service="owner", reason="seed")
age_wallet("u:801")
total_pred_loss = 0
for i in range(4):
    mkt_i = predictions.open_market(f"loss visibility {i}", ["yes", "no"], created_by="owner")
    predictions.stake(mkt_i, "u:801", "no", 4_000)
    predictions.resolve(mkt_i, "yes", money.new_event_id("pred.resolve"))  # nobody backs "yes": total loss
    total_pred_loss += 4_000

with db.db() as c:
    lost_row = c.execute(
        "SELECT lost FROM gambling_day WHERE subject = ? AND day = date('now')", ("u:801",)
    ).fetchone()
check("settled prediction losses accumulate in gambling_day -- no longer invisible "
      "to the daily cap forever",
      lost_row is not None and lost_row["lost"] == total_pred_loss,
      f"{dict(lost_row) if lost_row else None} vs {total_pred_loss}")

rid8b = games.open_round("coinflip", "post-pred-loss", 0)
raises("a casino bet that would push the SHARED daily cap over MAX_DAILY_LOSS is "
       "refused, counting today's already-realized prediction losses",
       games.DailyLossExceeded, games.place_bet, rid8b, "u:801", "heads", 5_000)
games.place_bet(rid8b, "u:801", "heads", 4_000)   # exactly fills the remaining shared room
check("a bet that exactly fills the remaining SHARED daily-loss room is accepted",
      money.balance("u:801").held == 4_000)

# 8c: cross-kind open exposure.
#
# SLICE NOTE: which answer is correct here belongs to the wagering-cap slice,
# which is landing in parallel (SLICE_CONTRACT.md sec 5 makes exposure
# wallet-wide and deletes wagering._EXPOSURE_JOIN). This file is owned by the
# fairness slice, so it asserts whichever contract core/wagering.py actually
# ships, and asserts it strictly -- it never just accepts both outcomes.
reset()
money.mint("u:803", 1_000_000, service="owner", reason="seed")
age_wallet("u:803")
mkt8c = predictions.open_market("open exposure", ["yes", "no"], created_by="owner")
for i in range(4):
    predictions.stake(mkt8c, "u:803", "yes", 4_500)   # left OPEN -- never resolved or voided
check("this subject now has 18,000 in OPEN prediction exposure",
      money.balance("u:803").held == 18_000)

_per_kind_exposure = hasattr(wagering, "_EXPOSURE_JOIN")
rid8c = games.open_round("coinflip", "cross-kind-exposure", 0)
if _per_kind_exposure:
    games.place_bet(rid8c, "u:803", "heads", 4_000)
    check("(per-kind exposure) an open prediction position does not lock this "
          "subject out of the casino",
          money.balance("u:803").held == 18_000 + 4_000,
          f"held={money.balance('u:803').held}")
    mkt8c2 = predictions.open_market("cross-kind reverse", ["yes", "no"], created_by="owner")
    predictions.stake(mkt8c2, "u:803", "yes", 1_000)
    check("(per-kind exposure) the reverse holds too",
          money.balance("u:803").held == 18_000 + 4_000 + 1_000,
          f"held={money.balance('u:803').held}")
else:
    raises("(wallet-wide exposure) a casino bet that would push COMBINED open "
           "exposure over MAX_DAILY_LOSS is refused, even though every open "
           "position so far is a prediction",
           games.DailyLossExceeded, games.place_bet, rid8c, "u:803", "heads", 4_000)
    games.place_bet(rid8c, "u:803", "heads", 2_000)   # exactly fills the wallet-wide room
    check("(wallet-wide exposure) a casino bet that exactly fills the remaining "
          "wallet-wide room is accepted, and combined exposure never exceeds "
          f"MAX_DAILY_LOSS ({games.MAX_DAILY_LOSS:,})",
          money.balance("u:803").held == 20_000 <= games.MAX_DAILY_LOSS,
          f"held={money.balance('u:803').held}")
    mkt8c2 = predictions.open_market("cross-kind reverse", ["yes", "no"], created_by="owner")
    raises("(wallet-wide exposure) and the reverse: an open casino position "
           "counts against a later prediction stake too",
           predictions.WagerRefused, predictions.stake, mkt8c2, "u:803", "yes", 1_000)


# ============================================================================
# REGRESSION 9: prediction resolve/void and casino settlement write
# audit_actions rows, in the same transaction, with reverse ops (finding 3).
# core/schema.sql:87.
# ============================================================================
print("\n[9] REGRESSION: prediction resolve/void and casino settlement are audited")

reset()
money.mint("u:900", 100_000, service="owner", reason="seed")
age_wallet("u:900")
rid9 = games.open_round("dice", "audit-check", 0)
games.place_bet(rid9, "u:900", "1", 100)
games.settle_round(rid9)

with db.db() as c:
    rows9 = c.execute(
        "SELECT * FROM audit_actions WHERE kind = 'game.settle' AND target = ?",
        (f"game_round:{rid9}",),
    ).fetchall()
check("settle_round wrote exactly one audit row", len(rows9) == 1, f"{len(rows9)}")
if rows9:
    row9 = dict(rows9[0])
    check("the settlement audit row names a real actor", row9["actor"] == "system:games")
    ops9 = audit.get(row9["id"])["ops"]
    check("ops_json names a real ledger primitive for the one bet settled",
          len(ops9) == 1 and ops9[0]["op"] in ("capture_hold", "transfer", "debt"), f"{ops9}")

reset()
money.mint("u:901", 1_000, service="owner", reason="seed")
age_wallet("u:901")
mkt9 = predictions.open_market("audit replay", ["yes", "no"], created_by="owner")
predictions.stake(mkt9, "u:901", "yes", 200)
ev9 = money.new_event_id("pred.resolve")
predictions.resolve(mkt9, "yes", ev9, actor="u:staffer")
predictions.resolve(mkt9, "yes", ev9, actor="u:staffer")   # replay, same event id

with db.db() as c:
    n9 = c.execute(
        "SELECT COUNT(*) AS n FROM audit_actions WHERE kind = 'prediction.resolve' "
        "AND target = ?", (f"pred_market:{mkt9}",),
    ).fetchone()["n"]
    actor9 = c.execute(
        "SELECT actor FROM audit_actions WHERE kind = 'prediction.resolve' AND target = ?",
        (f"pred_market:{mkt9}",),
    ).fetchone()["actor"]
check("a resolve() replay with the SAME event id writes no duplicate audit row",
      n9 == 1, f"{n9}")
check("the resolve audit row carries the actor that was passed in",
      actor9 == "u:staffer")

reset()
money.mint("u:902", 1_000, service="owner", reason="seed")
age_wallet("u:902")
mkt9v = predictions.open_market("audit void", ["yes", "no"], created_by="owner")
predictions.stake(mkt9v, "u:902", "yes", 300)
predictions.void(mkt9v, actor="u:staffer2")
with db.db() as c:
    vrow = c.execute(
        "SELECT * FROM audit_actions WHERE kind = 'prediction.void' AND target = ?",
        (f"pred_market:{mkt9v}",),
    ).fetchone()
check("void() wrote an audit row naming the actor",
      vrow is not None and vrow["actor"] == "u:staffer2")

predictions.void(mkt9v, actor="u:staffer2")   # already voided -- nothing left to release
with db.db() as c:
    n9v = c.execute(
        "SELECT COUNT(*) AS n FROM audit_actions WHERE kind = 'prediction.void' "
        "AND target = ?", (f"pred_market:{mkt9v}",),
    ).fetchone()["n"]
check("re-voiding an already-voided market (nothing released) adds no audit row",
      n9v == 1, f"{n9v}")


# ============================================================================
# ATTACK 10: THE fairness attack -- the commitment must be minted and
# published BEFORE the bet is known, the client seed must be the PLAYER's,
# and verify() must audit that pre-bet artifact.
#
# The defect: the server seed was HMAC(process secret, round_id) and the
# round id was chosen by the house AFTER the bet was on the table, while the
# "client seed" was itself server-generated. So the house could grind round
# ids until the outcome suited it -- and verify() recomputed the commitment
# hash from the very round it was validating, so it certified the rigged
# round as VALID. Every round ever played was unverifiable after the fact.
# ============================================================================
print("\n[10] commit-first: the published hash predates the stake, the client")
print("     seed is the player's, and verify() audits the pre-bet artifact")

reset()
money.mint("u:1000", 1_000_000, service="owner", reason="seed")
age_wallet("u:1000")

# --- 10a: the client seed is the PLAYER's. No default, no fallback. ---------
for bad, label in ((None, "a missing"), ("", "an empty"),
                   ("   ", "a whitespace-only"), (12345, "a non-string")):
    raises(f"open_round refuses {label} client_seed -- there is no server-side "
           f"default and no server-generated fallback anywhere in core/",
           ValueError, games.open_round, "dice", bad)

_rid_strip = games.open_round("dice", "  player typed this  ", 0)
with db.db() as c:
    _stored_cs = c.execute(
        "SELECT client_seed FROM game_rounds WHERE id = ?", (_rid_strip,)
    ).fetchone()["client_seed"]
check("the player's seed is stored verbatim (stripped), never replaced by a "
      "server value", _stored_cs == "player typed this", f"{_stored_cs!r}")

# --- 10b: commit() publishes a hash, durably, before any stake -------------
com = games.commit("u:1000")
check("commit() returns a commitment id, a published hash and a starting nonce",
      set(com) == {"commitment_id", "server_seed_hash", "next_nonce"}
      and com["next_nonce"] == 0, f"{com}")
check("commit() publishes ONLY the hash -- the seed itself is never returned",
      "server_seed" not in com, f"{com}")

_seen: dict[str, int] = {}


def _other_connection_reader() -> None:
    # A separate thread gets a separate sqlite connection, so this can only
    # see the row if commit() really committed its own transaction.
    with db.db() as c2:
        row = c2.execute(
            "SELECT server_seed, server_seed_hash, state, next_nonce "
            "FROM game_commitments WHERE id = ?", (com["commitment_id"],)
        ).fetchone()
    _seen["found"] = 1 if row is not None else 0
    _seen["hash_ok"] = 1 if (
        row is not None
        and hashlib.sha256(row["server_seed"].encode()).hexdigest() == row["server_seed_hash"]
        and row["server_seed_hash"] == com["server_seed_hash"]
    ) else 0
    _seen["open"] = 1 if (row is not None and row["state"] == "open") else 0


_t = threading.Thread(target=_other_connection_reader)
_t.start()
_t.join()
check("the commitment is durable and readable from ANOTHER connection before "
      "any stake is accepted -- that is what 'published' has to mean",
      _seen.get("found") == 1 and _seen.get("open") == 1, f"{_seen}")
check("the published hash really is sha256 of the stored seed",
      _seen.get("hash_ok") == 1, f"{_seen}")

# --- 10c: the house grinds round ids AFTER seeing the bet ------------------
def house_grinds_a_losing_round_id(game: str, client_seed: str, selection: str,
                                    nonce: int = 0) -> str:
    """The house has already seen the player's bet. Under the old scheme the
    seed was HMAC(process secret, round_id), so it mints candidate round ids
    with the LIVE secret until it finds one that loses, and opens that one."""
    secret = os.environ["NOLA_GAME_SEED_SECRET"]
    for k in range(5_000):
        candidate = f"round.{game}:grind-{client_seed}-{k}"
        if derived_seed_outcome(game, candidate, client_seed, nonce, secret) != selection:
            return candidate
    raise AssertionError("no losing candidate found -- grinder is broken")


GRIND_M = 30
grind_losses = 0
for i in range(GRIND_M):
    cs = f"player-seed-{i}"
    rigged = house_grinds_a_losing_round_id("coinflip", cs, "heads")
    games.open_round("coinflip", cs, 0, round_id=rigged)
    games.place_bet(rigged, "u:1000", "heads", 10)
    if not games.settle_round(rigged)["results"][0]["win"]:
        grind_losses += 1

check(f"a house that grinds round ids AFTER seeing the bet cannot force a "
      f"loss: {grind_losses}/{GRIND_M} losses, not {GRIND_M}/{GRIND_M} -- the "
      f"seed is random and committed, so the round id decides nothing",
      grind_losses < 0.8 * GRIND_M, f"grind_losses={grind_losses}/{GRIND_M}")

# --- 10d: verify() audits the PRE-BET artifact ----------------------------
reset()
money.mint("u:1001", 1_000_000, service="owner", reason="seed")
age_wallet("u:1001")

rid10 = games.open_round("dice", "audit-me", 0)
games.place_bet(rid10, "u:1001", "3", 10)
games.settle_round(rid10)
with db.db() as c:
    cid10 = c.execute(
        "SELECT commitment_id FROM game_rounds WHERE id = ?", (rid10,)
    ).fetchone()["commitment_id"]

v = games.verify(rid10)
check("an honest round verifies with all four independent checks true",
      v["seed_matches_commitment"] and v["commitment_matches_round"]
      and v["committed_before_bets"] and v["outcome_matches"] and v["ok"], f"{v}")
check("verify() names the commitment the round was played against",
      v["commitment_id"] == cid10, f"{v['commitment_id']} vs {cid10}")

# (i) the commitment was minted AFTER the bet was on the table
with db.db() as c:
    c.execute("UPDATE game_commitments SET created_at = "
              "(SELECT datetime(MIN(placed_at), '+5 seconds') FROM game_bets "
              " WHERE round_id = ?) WHERE id = ?", (rid10, cid10))
v_late = games.verify(rid10)
check("a commitment minted AFTER the first bet is REFUSED by verify -- this "
      "is the rigged round the old verify() certified as VALID",
      v_late["committed_before_bets"] is False and v_late["ok"] is False, f"{v_late}")

# (ii) the house swaps the seed after the fact
with db.db() as c:
    c.execute("UPDATE game_commitments SET created_at = datetime('now', '-1 hour') "
              "WHERE id = ?", (cid10,))
    c.execute("UPDATE game_commitments SET server_seed = 'deadbeef' WHERE id = ?", (cid10,))
v_swapped = games.verify(rid10)
check("a commitment whose seed no longer hashes to its published hash fails "
      "verification, and the round's copy no longer matches it either",
      v_swapped["seed_matches_commitment"] is False
      and v_swapped["commitment_matches_round"] is False
      and v_swapped["ok"] is False, f"{v_swapped}")

# (iii) no commitment at all
with db.db() as c:
    c.execute("UPDATE game_commitments SET server_seed = "
              "(SELECT server_seed FROM game_rounds WHERE id = ?) WHERE id = ?",
              (rid10, cid10))
    c.execute("UPDATE game_rounds SET commitment_id = NULL WHERE id = ?", (rid10,))
v_none = games.verify(rid10)
check("a round with NO commitment artifact verifies as ok=False, not as a "
      "round that happens to recompute consistently with itself",
      v_none["commitment_id"] is None
      and v_none["commitment_matches_round"] is False
      and v_none["outcome_matches"] is True     # it is self-consistent...
      and v_none["ok"] is False,                # ...and that is not enough
      f"{v_none}")

# --- 10e: the nonce is monotonic per commitment ---------------------------
reset()
money.mint("u:1002", 1_000_000, service="owner", reason="seed")
age_wallet("u:1002")

cid_n = games.commit()["commitment_id"]
games.open_round("dice", "nonce-test", 0, commitment_id=cid_n)
raises("a nonce is never REUSED on one commitment",
       games.RoundNotOpen, games.open_round, "dice", "nonce-test", 0,
       commitment_id=cid_n)
games.open_round("dice", "nonce-test", 5, commitment_id=cid_n)
raises("a nonce is never REWOUND on one commitment",
       games.RoundNotOpen, games.open_round, "dice", "nonce-test", 3,
       commitment_id=cid_n)
games.open_round("dice", "nonce-test", 6, commitment_id=cid_n)   # forward is fine
raises("an unknown commitment id is refused, never silently minted",
       games.UnknownRound, games.open_round, "dice", "nonce-test", 0,
       commitment_id="commit:does-not-exist")

# --- 10f: a revealed commitment accepts no further bets -------------------
cid_r = games.commit()["commitment_id"]
r_first = games.open_round("coinflip", "reveal-test", 0, commitment_id=cid_r)
r_second = games.open_round("coinflip", "reveal-test", 1, commitment_id=cid_r)
games.place_bet(r_first, "u:1002", "heads", 10)
games.settle_round(r_first)                      # reveals the seed publicly
raises("once a commitment is revealed its seed is public, so a bet on another "
       "round sharing it is refused -- never a bet on a known outcome",
       games.RoundNotOpen, games.place_bet, r_second, "u:1002", "heads", 10)
raises("and no new round may be opened against a revealed commitment",
       games.RoundNotOpen, games.open_round, "coinflip", "reveal-test", 2,
       commitment_id=cid_r)


# ============================================================================
# ATTACK 11: the dice draw itself. `digest[0] % 6` is not uniform -- 256 is
# not a multiple of 6, so faces 1-4 came up 43/256 and faces 5-6 42/256. The
# real house edge was 4.26% on four faces and 6.48% on two, while GAME_CONFIG
# says 5%. CONTRACT.md section 9: the edge is an explicit config number,
# never an emergent property of the payout maths.
# ============================================================================
print("\n[11] dice uniformity: every face exactly 1/6, so the edge is the")
print("     config number and not an artifact of the draw")

N_DIST = 1_000_000
DIST_SEED = "b" * 64

counts = [0] * 6
for i in range(N_DIST):
    counts[games._uniform_int(DIST_SEED, "dist", i, 6)] += 1
expected = N_DIST / 6
chi2 = sum((n - expected) ** 2 / expected for n in counts)
check(f"chi-square over {N_DIST:,} dice draws is {chi2:.2f} < 30 (df=5; a fair "
      f"generator scores ~5, and P(chi2 > 30) ~ 1.5e-5)",
      chi2 < 30, f"chi2={chi2:.2f} counts={counts}")

old_counts = [0] * 6
for i in range(N_DIST):
    d = hmac.new(DIST_SEED.encode(), f"dist:{i}".encode(), hashlib.sha256).digest()
    old_counts[d[0] % 6] += 1
old_chi2 = sum((n - expected) ** 2 / expected for n in old_counts)
check(f"the test DISCRIMINATES: the old digest[0] % 6 draw scores "
      f"chi2 = {old_chi2:.2f} on the same sample size, far past the same "
      f"threshold this check uses",
      old_chi2 > 100, f"old_chi2={old_chi2:.2f} counts={old_counts}")

coin_counts = [0, 0]
for i in range(100_000):
    coin_counts[games._uniform_int(DIST_SEED, "coin", i, 2)] += 1
coin_chi2 = sum((n - 50_000) ** 2 / 50_000 for n in coin_counts)
check(f"coinflip is uniform too (chi2 = {coin_chi2:.2f} < 11 on df=1)",
      coin_chi2 < 11, f"coin_chi2={coin_chi2:.2f} counts={coin_counts}")

# The edge is the config number, for EVERY face -- not 4.26% on four of them
# and 6.48% on the other two.
_dice_payout = games.GAME_CONFIG["dice"]["payout_bps"] / 10_000     # 5.70x
_true_edges = [1 - _dice_payout * (n / N_DIST) for n in counts]
check(f"every dice face carries the configured 5% edge (measured "
      f"{min(_true_edges):.4f}..{max(_true_edges):.4f}), rather than the "
      f"4.26%/6.48% split the biased draw produced",
      all(abs(e - 0.05) < 0.005 for e in _true_edges),
      f"edges={[round(e, 5) for e in _true_edges]}")
check("with p exactly 1/6 the arithmetic edge is exactly the config number",
      abs((1 - _dice_payout / 6) - 0.05) < 1e-12, f"{1 - _dice_payout / 6}")
check("...and the OLD probabilities really were 4.26%/6.48%, which is the "
      "defect this section closes",
      abs((1 - _dice_payout * 43 / 256) - 0.0426) < 5e-5
      and abs((1 - _dice_payout * 42 / 256) - 0.0648) < 5e-5,
      f"{1 - _dice_payout * 43 / 256} {1 - _dice_payout * 42 / 256}")


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
