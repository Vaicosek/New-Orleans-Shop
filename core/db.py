"""Database access for New Orleans.

One SQLite file, WAL, foreign keys ON, thread-local connections.

The two context managers exist for a reason that cost six rounds to find in the
system this is descended from: `db()` opens a transaction, and `db_in(conn)`
JOINS the caller's if it was given one. Nesting a fresh `db()` inside an already
open transaction commits the caller's half-written work early. Every function
below the API surface takes an optional `conn` and passes it down.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
# `.get(..., default)` only helps when the key is ABSENT -- a `.env` line
# left blank on purpose (Wispbyte's panel ships one: `NOLA_DB_PATH=`) sets
# the key to '', which `.get` happily returns instead of the default,
# giving `Path("")` == the current directory. sqlite3 then fails to open
# that directory as a file with an unhelpful "unable to open database
# file" -- exactly the outage this `or` exists to prevent.
DB_PATH = Path(os.environ.get("NOLA_DB_PATH") or (ROOT / "neworleans.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_local = threading.local()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _connect() -> sqlite3.Connection:
    conn = _open()
    if not _try_journal_mode(conn, "WAL"):
        # The failed PRAGMA leaves the connection unusable for the rest of
        # this session -- a later write still raises "disk I/O error" even
        # though the mode was never actually changed. Reopen rather than
        # carry the damaged handle forward; reusing it is what made this
        # look like a schema bug instead of a filesystem one.
        conn.close()
        print(f"[db] WAL unavailable under {DB_PATH.parent}; using TRUNCATE journalling. "
              f"Expected on a mounted or network folder, NOT expected in production.",
              flush=True)
        conn = _open()
        if not _try_journal_mode(conn, "TRUNCATE"):
            conn.close()
            raise RuntimeError(
                f"{DB_PATH} is on a filesystem that supports neither WAL nor TRUNCATE "
                f"journalling. SQLite cannot run here -- put the database somewhere else "
                f"with NOLA_DB_PATH."
            )
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _try_journal_mode(conn: sqlite3.Connection, mode: str) -> bool:
    """Ask for a journal mode. True if the database actually took it.

    Returns rather than raises, and CHECKS the value back: SQLite answers a
    journal_mode request with the mode now in force, which is not always the
    one asked for. Trusting the request instead of the answer is how a
    database ends up running in a durability mode nobody chose.
    """
    try:
        got = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
    except sqlite3.OperationalError:
        return False
    return str(got).lower() == mode.lower()


def connection() -> sqlite3.Connection:
    """The calling thread's connection, opened on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = _connect()
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Open a transaction. Commits on clean exit, rolls back on any exception."""
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def db_in(conn: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    """Join the caller's transaction if given one, otherwise open our own.

    Pass `conn` down through every layer. A function that opens its own
    transaction when its caller already has one will commit the caller's
    partial work, and the resulting bug looks like a race rather than a nesting
    mistake.
    """
    if conn is not None:
        yield conn
        return
    with db() as own:
        yield own


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    """Apply the schema (idempotent) and bring up the fixed treasury wallets.

    This is the ONE shared boot path both entrypoints call before doing
    anything else -- `run_shop.py` via `bot.main.main()`, `run_web.py`
    directly -- which makes it also the one place that can guarantee
    `treasury:shop` / `treasury:games` / `treasury:house` exist, with their
    deficit floors, before a single order, bet or stake can ever be placed
    against them. Previously nothing called `money.bootstrap_treasuries` at
    all, so those wallets only ever came into being lazily (auto-vivified at
    floor 0 by the first `ensure_wallet` an unrelated transfer happened to
    trigger) -- ordering that a boot path must not leave to chance.

    `bootstrap_treasuries` is exactly as idempotent as the rest of this
    function: it only ever inserts a missing wallet or (re-)applies a floor,
    never touches a balance, so calling it on every boot (including inside a
    test's own `db.init_db()`) is safe.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    target = conn if conn is not None else connection()
    target.executescript(sql)
    _migrate(target)
    from . import money  # local import: money imports back from this module
    money.bootstrap_treasuries(conn=target)


# Migrations are content-probed and idempotent rather than version-numbered.
# Each entry says WHY it exists — there is no schema_version table to anchor an
# external changelog to, so the reason has to live next to the statement.
_MIGRATIONS: list[str] = [
    # audit_actions grew a `target` column (who/what an action was done to)
    # after shipping without one -- CONTRACT.md sec 4 promises "who did it",
    # which is meaningless without also saying to whom.
    "ALTER TABLE audit_actions ADD COLUMN target TEXT NOT NULL DEFAULT ''",
    # game_rounds gained `commitment_id` when the casino moved to published
    # commitments. schema.sql uses CREATE TABLE IF NOT EXISTS, so an existing
    # database never picks a new column up from the DDL -- only from here.
    # Nullable on purpose: rounds played before the change have no commitment
    # and must verify as ok=False rather than pretend to one.
    "ALTER TABLE game_rounds ADD COLUMN commitment_id TEXT",
]


# game_rounds.game's CHECK constraint gained 'slots' when the third casino
# game shipped. SQLite cannot ALTER a CHECK constraint in place, so an
# existing database needs the table rebuilt: create it fresh under the new
# (wider) CHECK, copy every row across untouched, then swap names. This DDL
# is a duplicate of schema.sql's game_rounds definition on purpose -- same
# convention core/games.py's `_ensure_schema`/`_COMMITMENTS_DDL` already
# uses for a content-probed, idempotent fallback -- and the two must be kept
# in sync if game_rounds ever gains another column or constraint.
_GAME_ROUNDS_REBUILD_DDL = """
CREATE TABLE game_rounds (
    id               TEXT    PRIMARY KEY,
    game             TEXT    NOT NULL CHECK (game IN ('coinflip', 'dice', 'slots')),
    server_seed_hash TEXT    NOT NULL,
    server_seed      TEXT,
    client_seed      TEXT    NOT NULL,
    nonce            INTEGER NOT NULL,
    outcome_json     TEXT,
    state            TEXT    NOT NULL DEFAULT 'open'
                             CHECK (state IN ('open', 'settled', 'voided')),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at       TEXT,
    commitment_id    TEXT,
    CHECK ((state = 'settled') = (server_seed IS NOT NULL AND outcome_json IS NOT NULL))
)
"""


def _migrate_game_rounds_check(conn: sqlite3.Connection) -> None:
    """Content-probed like every migration here: reads game_rounds' own
    CREATE SQL out of sqlite_master and does nothing if it already allows
    'slots' -- true immediately on any fresh database, since schema.sql's
    own CREATE TABLE IF NOT EXISTS already created it with the wide CHECK
    before this function ever runs; only a database created before slots
    shipped needs the rebuild below.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_rounds'"
    ).fetchone()
    if row is None or row["sql"] is None or "slots" in row["sql"]:
        return
    # Two pragmas matter here, not one. `foreign_keys=OFF` stops
    # game_bets' ON DELETE CASCADE from firing while the table is briefly
    # gone. `legacy_alter_table=ON` matters MORE and is easy to miss: by
    # default (OFF, since SQLite 3.25) `ALTER TABLE ... RENAME` REWRITES
    # every other table's FOREIGN KEY clause that names the renamed table --
    # so a plain rename of game_rounds to a scratch name silently repoints
    # game_bets.round_id at that scratch name, and it stays pointed there
    # after the scratch table is dropped, leaving a dangling reference
    # `PRAGMA foreign_key_check` flags but nothing else notices until a
    # cascade or a join quietly stops working. `legacy_alter_table=ON`
    # reverts RENAME to the old behaviour (touches only the named table),
    # so game_bets' clause stays literally "REFERENCES game_rounds(id)"
    # throughout and resolves again the moment the real name exists.
    # Caught by tests/test_slots.py running this exact migration against a
    # pre-slots table with a real referencing row, not by inspection.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("ALTER TABLE game_rounds RENAME TO game_rounds_pre_slots")
        conn.execute(_GAME_ROUNDS_REBUILD_DDL)
        conn.execute(
            "INSERT INTO game_rounds "
            "(id, game, server_seed_hash, server_seed, client_seed, nonce, "
            " outcome_json, state, created_at, settled_at, commitment_id) "
            "SELECT id, game, server_seed_hash, server_seed, client_seed, nonce, "
            "       outcome_json, state, created_at, settled_at, commitment_id "
            "FROM game_rounds_pre_slots"
        )
        conn.execute("DROP TABLE game_rounds_pre_slots")
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as err:
            # "duplicate column name" means this migration already ran.
            if "duplicate column" not in str(err).lower():
                raise
    _migrate_game_rounds_check(conn)


def seed_if_empty(conn: Optional[sqlite3.Connection] = None) -> int:
    """Load the catalog if there isn't one. Returns rows seeded (0 if none).

    Wispbyte gives no shell, so `seed_catalog.py` cannot be run on the
    server. Without this, a database created fresh on the panel comes up
    with an empty catalog and the only way to fill it is typing every item
    into a modal by hand.

    Deliberately narrow: it fires ONLY when `items` is completely empty, so
    it can never overwrite a price the owner has since changed, and it never
    touches balances, orders or the ledger. It is loud either way -- a silent
    seed would be indistinguishable from a database that quietly lost its
    catalog.
    """
    with db_in(conn) as c:
        existing = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
    if existing:
        return 0
    seed = ROOT / "seed_catalog.py"
    if not seed.exists():
        print("[db] catalog is empty and seed_catalog.py is missing -- "
              "add items through the admin panel.", flush=True)
        return 0
    import io
    import contextlib
    import runpy
    print("[db] catalog is empty -- seeding from seed_catalog.py", flush=True)
    with contextlib.redirect_stdout(io.StringIO()):
        runpy.run_path(str(seed), run_name="__main__")
    with db_in(conn) as c:
        now = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
    print(f"[db] seeded {now} catalog items", flush=True)
    return now


def get_config(key: str, default: str | None = None,
               conn: Optional[sqlite3.Connection] = None) -> str | None:
    with db_in(conn) as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str, conn: Optional[sqlite3.Connection] = None) -> None:
    with db_in(conn) as c:
        c.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value)),
        )
