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
DB_PATH = Path(os.environ.get("NOLA_DB_PATH", ROOT / "neworleans.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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
    """Apply the schema. Idempotent — every statement is CREATE ... IF NOT EXISTS."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    target = conn if conn is not None else connection()
    target.executescript(sql)
    _migrate(target)


# Migrations are content-probed and idempotent rather than version-numbered.
# Each entry says WHY it exists — there is no schema_version table to anchor an
# external changelog to, so the reason has to live next to the statement.
_MIGRATIONS: list[str] = [
    # (none yet — the schema is new. Append ALTER TABLE statements here.)
]


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as err:
            # "duplicate column name" means this migration already ran.
            if "duplicate column" not in str(err).lower():
                raise


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
