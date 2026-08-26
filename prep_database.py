"""Create and populate the live New Orleans database.

Safe to re-run: the schema is CREATE ... IF NOT EXISTS, the treasuries are
upserted, and the catalog seed updates rather than duplicating. It will NOT
overwrite balances, orders, holds or ledger history.

    python prep_database.py            # create/refresh, then verify
    python prep_database.py --verify   # check only, change nothing

The bot calls init_db() itself at boot, so this is not required for the bot
to run -- it exists so the database is known-good and stocked BEFORE the
first person types a command, rather than being created empty underneath
them.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import db, money  # noqa: E402
from core.pricing import CURRENCY  # noqa: E402

VERIFY_ONLY = "--verify" in sys.argv

EXPECTED_TABLES = {
    "wallets", "wallet_flags", "ledger_entries", "ledger_holds", "idempotency",
    "audit_actions", "items", "stock", "stock_alerts", "orders", "order_claims",
    "addresses", "pred_markets", "pred_outcomes", "pred_stakes", "game_rounds",
    "game_bets", "gambling_day", "config", "web_sessions", "categories",
}

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        problems.append(label)


print(f"database: {db.DB_PATH}")
existed = db.DB_PATH.exists()
print(f"          {'already present -- refreshing in place' if existed else 'creating'}\n")

if not VERIFY_ONLY:
    db.init_db()                     # schema + migrations + treasuries
    import runpy
    runpy.run_path(str(ROOT / "seed_catalog.py"), run_name="__main__")
    print()

print("verifying")
with db.db() as c:
    # --- structure
    tables = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = EXPECTED_TABLES - tables
    check(f"all {len(EXPECTED_TABLES)} tables present", not missing, f"missing: {sorted(missing)}")

    # --- pragmas. A copy of the data is not a copy of the environment: a
    # migration that passed with foreign_keys off has failed in production
    # with it on. Assert the environment, not just the rows.
    fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
    jm = c.execute("PRAGMA journal_mode").fetchone()[0]
    check("foreign keys enforced", bool(fk), f"foreign_keys={fk}")
    check("write-ahead logging on", str(jm).lower() == "wal", f"journal_mode={jm}")

    fk_broken = c.execute("PRAGMA foreign_key_check").fetchall()
    check("no broken foreign keys", not fk_broken, f"{len(fk_broken)} violations")
    integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    check("integrity check clean", integrity == "ok", str(integrity))

    # --- treasuries
    treas = {r["subject"]: r for r in c.execute(
        "SELECT subject, coins, deficit_floor FROM wallets WHERE subject LIKE 'treasury:%'")}
    for name in ("treasury:shop", "treasury:games", "treasury:house"):
        check(f"{name} exists", name in treas)

    # --- catalog
    items = c.execute("SELECT COUNT(*) n FROM items WHERE active = 1").fetchone()["n"]
    cats = c.execute("SELECT COUNT(*) n FROM categories").fetchone()["n"]
    stocked = c.execute("SELECT COUNT(*) n FROM stock WHERE pieces > 0").fetchone()["n"]
    check("catalog has items", items > 0, f"{items}")
    check("categories declared", cats > 0, f"{cats}")

    # --- the unit split. One wrong row here is a silent 2x on every sale of
    # that item, and nothing crashes.
    bad_unit = c.execute(
        "SELECT COUNT(*) n FROM items WHERE price_unit_pieces > stack_size").fetchone()["n"]
    bad_stack = c.execute(
        "SELECT COUNT(*) n FROM items WHERE stackable = 0 AND stack_size <> 1").fetchone()["n"]
    check("no item priced per more pieces than a stack holds", bad_unit == 0, f"{bad_unit} rows")
    check("no non-stackable item with a stack size", bad_stack == 0, f"{bad_stack} rows")

    # --- money conservation
    led = c.execute("SELECT COALESCE(SUM(delta),0) t FROM ledger_entries").fetchone()["t"]
    wal_ = c.execute("SELECT COALESCE(SUM(coins),0) t FROM wallets").fetchone()["t"]
    check("ledger sums to the money in the system", led == wal_, f"ledger {led} vs wallets {wal_}")

    unreasoned = c.execute(
        "SELECT COUNT(*) n FROM ledger_entries WHERE trim(reason) = ''").fetchone()["n"]
    check("every ledger entry has a reason", unreasoned == 0, f"{unreasoned} unreasoned")

    orphan_holds = c.execute(
        "SELECT COUNT(*) n FROM ledger_holds h LEFT JOIN wallets w ON w.subject = h.subject "
        " WHERE w.subject IS NULL").fetchone()["n"]
    check("no holds against a missing wallet", orphan_holds == 0, f"{orphan_holds}")

print()
with db.db() as c:
    rows = c.execute(
        "SELECT category, COUNT(*) n, COALESCE(SUM(slots),0) s FROM items "
        " WHERE active = 1 GROUP BY category ORDER BY MIN(sort_order), category").fetchall()
    print("catalog")
    for r in rows:
        print(f"  {r['category'] or '(none)':<22} {r['n']:>3} items   {r['s']:>3} slots")
    planned = c.execute(
        "SELECT name FROM categories WHERE name NOT IN "
        "(SELECT DISTINCT category FROM items WHERE active = 1) ORDER BY sort_order").fetchall()
    if planned:
        print("  planned, no items yet: " + ", ".join(r["name"] for r in planned))
    print()
    print("treasuries")
    for name, r in sorted(treas.items()):
        print(f"  {name:<18} {r['coins']:>10,} {CURRENCY}   floor {r['deficit_floor']:,}")

print()
if problems:
    print(f"{len(problems)} PROBLEM(S): " + "; ".join(problems))
    raise SystemExit(1)
print("database ready" + ("" if stocked else "  (no stock recorded yet -- expected until the first count)"))
