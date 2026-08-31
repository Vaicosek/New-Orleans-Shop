"""Slots -- provably-fair, three independent reels, pays on 3-of-a-kind only.

Same style as test_betting.py: real SQLite files, no mocks, check()/raises(),
exit 1 on any failure. This file exists specifically to pin the things
core/games.py's own comments point back at:

  [1] RTP/edge re-derived algebraically from GAME_CONFIG["slots"] itself, so
      a future edit to reel_strip or payout_table cannot silently drift the
      edge the way the old `digest[0] % 6` dice bug did.
  [2] a full play() round trip (commit -> bet -> settle -> verify) for
      slots specifically, not just coinflip/dice.
  [3] _max_payout_bps uses the JACKPOT multiplier for the solvency
      pre-check, not some other entry in the payout table.
  [4] the game_rounds CHECK-constraint migration (core/db.py's
      _migrate_game_rounds_check) actually accepts 'slots' end to end
      through the real db.init_db() path, and the FK from game_bets to
      game_rounds survives it (the legacy_alter_table footgun).
  [5] reel draws are independent per position -- not the same draw repeated
      three times -- verified two ways: a live sample and a fixed-vector
      recompute against _uniform_int_positioned directly.
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections import Counter
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="nola-slots-test-")
os.environ["NOLA_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["NOLA_GAME_SEED_SECRET"] = "test-secret-do-not-use-in-prod"

from core import db, money, games                            # noqa: E402

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
        for t in ("game_bets", "game_rounds", "ledger_entries", "ledger_holds",
                  "wallet_flags", "idempotency", "gambling_day", "wallets"):
            c.execute(f"DELETE FROM {t}")
    money.ensure_wallet("treasury:games", deficit_floor=1_000_000, service="owner")


def age_wallet(subject: str, days: int = 30) -> None:
    with db.db() as c:
        c.execute(
            "UPDATE wallets SET created_at = datetime('now', ?) WHERE subject = ?",
            (f"-{days} days", subject),
        )


db.init_db()

# ------------------------------------------------------------------ [1] RTP / edge, re-derived
print("\nRTP and edge re-derived algebraically from GAME_CONFIG['slots']")
cfg = games.GAME_CONFIG["slots"]
strip = cfg["reel_strip"]
payout_table = cfg["payout_table"]
reels = cfg["reels"]
total_weight = sum(strip.values())

rtp = Fraction(0)
win_prob = Fraction(0)
for symbol, weight in strip.items():
    p_symbol_all_reels = Fraction(weight, total_weight) ** reels
    win_prob += p_symbol_all_reels
    if symbol in payout_table:
        rtp += p_symbol_all_reels * Fraction(payout_table[symbol], 10_000)

edge = 1 - rtp

check("every symbol on the strip has a payout entry",
      set(strip) == set(payout_table), f"{set(strip)} vs {set(payout_table)}")
check("RTP is 91.975% exactly", rtp == Fraction(3679, 4000), str(rtp))
check("edge is 8.025% exactly", edge == Fraction(321, 4000), str(edge))
check("win probability (any 3-of-a-kind) is 12.25% exactly",
      win_prob == Fraction(49, 400), str(win_prob))
check("the jackpot symbol is the rarest one on the strip",
      min(strip, key=strip.get) == max(payout_table, key=payout_table.get))

# ------------------------------------------------------------------ [2] full round trip
print("\nslots: full round end to end (commit -> bet -> settle -> verify)")
reset()
money.mint("u:1", 10_000, service="owner", reason="seed")
age_wallet("u:1")

result = games.play("u:1", "slots", "spin", 100, client_seed="alice-seed", nonce=1)
check("play returns a round id and outcome", "round_id" in result and "outcome" in result)
check("round records the right game", result["game"] == "slots")
outcome_reels = result["outcome"]["reels"]
check("outcome carries exactly 3 reels", len(outcome_reels) == reels, str(outcome_reels))
check("every reel symbol is a real strip symbol", all(r in strip for r in outcome_reels))

bal = money.balance("u:1")
win = result["results"][0]["win"]
if win:
    check("winner's balance grew by (payout - stake)",
          bal.coins == 10_000 + (result["results"][0]["payout"] - 100),
          f"coins={bal.coins}")
else:
    check("loser's balance shrank by exactly the stake", bal.coins == 9_900, f"coins={bal.coins}")
check("nothing stays held after settlement", bal.held == 0)

v = games.verify(result["round_id"])
check("verify confirms the seed matches its commitment", v["seed_matches_commitment"])
check("verify recomputes the same reels", v["outcome_matches"])
check("verify is overall ok", v["ok"])

import json as _json                                          # noqa: E402
with db.db() as c:
    c.execute("UPDATE game_rounds SET outcome_json = ? WHERE id = ?",
              (_json.dumps({"reels": ["seven", "seven", "cherry"]}), result["round_id"]))
tampered = games.verify(result["round_id"])
check("verify catches a tampered reel outcome", not tampered["outcome_matches"] and not tampered["ok"])

# ------------------------------------------------------------------ [2b] three-of-a-kind actually pays the right multiplier
print("\nslots: payout matches the symbol actually landed, not a flat rate")
reset()
money.mint("u:2", 10_000_000, service="owner", reason="seed")
age_wallet("u:2")
matched = {"cherry": None, "lemon": None, "bell": None, "seven": None}
nonce = 0
# Search a bounded number of nonces for at least one round landing each
# symbol 3x -- this is a live confirmation that settle_round prices by the
# ACTUAL outcome (via _payout_bps), not GAME_CONFIG's max.
for nonce in range(2000):
    rid = games.open_round("slots", "hunt-seed", nonce)
    games.place_bet(rid, "u:2", "spin", 10)
    r = games.settle_round(rid)
    bet = r["results"][0]
    with db.db() as c:
        row = c.execute("SELECT outcome_json FROM game_rounds WHERE id = ?", (rid,)).fetchone()
    landed = _json.loads(row["outcome_json"])["reels"]
    if len(set(landed)) == 1 and matched[landed[0]] is None:
        matched[landed[0]] = bet["payout"]
    if all(v is not None for v in matched.values()):
        break

for symbol, payout in matched.items():
    expected = 10 * payout_table[symbol] // 10_000
    check(f"a landed {symbol!r} 3-of-a-kind pays {payout_table[symbol] / 10_000:g}x",
          payout is None or payout == expected,
          f"payout={payout} expected={expected}")
check("all four symbols were observed to land and pay correctly within the search budget",
      all(v is not None for v in matched.values()), str(matched))

# ------------------------------------------------------------------ [3] solvency pre-check uses the jackpot
print("\nplace_bet's solvency pre-check uses the JACKPOT multiplier for slots")
check("_max_payout_bps(slots) is the jackpot (seven, 400x)",
      games._max_payout_bps("slots") == payout_table["seven"] == 4_000_000)
check("_max_payout_bps(coinflip/dice) is unchanged (flat payout_bps)",
      games._max_payout_bps("coinflip") == games.GAME_CONFIG["coinflip"]["payout_bps"]
      and games._max_payout_bps("dice") == games.GAME_CONFIG["dice"]["payout_bps"])

reset()
# treasury:games starts with only a small deficit_floor -- a bet whose
# worst case (jackpot) the treasury could not fund must be refused BEFORE
# any hold is placed, exactly like the existing MAX_BET/insolvency guards
# for coinflip/dice in test_betting.py.
with db.db() as c:
    c.execute("UPDATE wallets SET coins = 0 WHERE subject = 'treasury:games'")
money.mint("u:3", 1_000_000, service="owner", reason="seed")
age_wallet("u:3")
rid3 = games.open_round("slots", "solvency-seed", 0)
big_bet = games.MAX_BET
worst_case_profit = (big_bet * payout_table["seven"]) // 10_000 - big_bet
if worst_case_profit > 0:
    raises("a slots bet whose jackpot the treasury could not cover is refused",
           games.HouseInsolvent, games.place_bet, rid3, "u:3", "spin", big_bet)
    check("the refused bet left no hold behind", money.balance("u:3").held == 0)
else:
    check("worst-case profit was non-positive; solvency check not exercised here", True)

# ------------------------------------------------------------------ [4] CHECK-constraint migration end to end
print("\ngame_rounds accepts 'slots' via the real db.init_db() path, FK intact")
with db.db() as c:
    create_sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_rounds'"
    ).fetchone()["sql"]
check("the live game_rounds CHECK constraint allows 'slots'", "slots" in create_sql, create_sql)

reset()
money.mint("u:4", 1000, service="owner", reason="seed")
age_wallet("u:4")
rid4 = games.play("u:4", "slots", "spin", 10, client_seed="fk-check", nonce=1)["round_id"]
with db.db() as c:
    fk_problems = c.execute("PRAGMA foreign_key_check").fetchall()
    bet_row = c.execute(
        "SELECT round_id FROM game_bets WHERE round_id = ?", (rid4,)
    ).fetchone()
check("no dangling foreign keys after a slots round settles", len(fk_problems) == 0, str(fk_problems))
check("the bet's round_id still resolves to a real game_rounds row", bet_row is not None)

def _insert_bad_game() -> None:
    with db.db() as c:
        c.execute(
            "INSERT INTO game_rounds (id, game, server_seed_hash, server_seed, client_seed, "
            "nonce, outcome_json, state, created_at) VALUES "
            "('bad-row', 'roulette', 'x', 'x', 'x', 0, '{}', 'open', datetime('now'))"
        )


import sqlite3 as _sqlite3                                    # noqa: E402
raises("the CHECK constraint still rejects an unknown game name",
       _sqlite3.IntegrityError, _insert_bad_game)

# ------------------------------------------------------------------ [5] independent per-reel draws
print("\nreels are drawn independently per position, not one draw repeated 3x")
draws_by_position = [
    games._uniform_int_positioned("some-server-seed", "client", 7, total_weight, pos)
    for pos in range(reels)
]
check("the three positions do not all draw the same index (fixed vector)",
      len(set(draws_by_position)) > 1 or total_weight <= 1, str(draws_by_position))

reset()
money.mint("u:5", 5_000_000, service="owner", reason="seed")
age_wallet("u:5")
symbol_counts = [Counter() for _ in range(reels)]
for n in range(300):
    rid = games.open_round("slots", "indep-seed", n)
    games.place_bet(rid, "u:5", "spin", 1)
    games.settle_round(rid)
    with db.db() as c:
        row = c.execute("SELECT outcome_json FROM game_rounds WHERE id = ?", (rid,)).fetchone()
    landed = _json.loads(row["outcome_json"])["reels"]
    for pos, sym in enumerate(landed):
        symbol_counts[pos][sym] += 1
check("every reel position independently produced more than one distinct symbol "
      "over 300 spins (a real per-position draw, not a constant)",
      all(len(c) > 1 for c in symbol_counts), str(symbol_counts))


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all slots tests pass")
