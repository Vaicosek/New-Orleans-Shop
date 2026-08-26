"""Short address codes for `/go <code>`.

No core/ module owns `addresses` -- it is plumbing (a lookup table, like
`config`), not domain logic, so minting and resolving codes lives here
rather than inside catalog/orders/predictions/games. Nothing in this module
computes a price, moves a coin, or decides an outcome; it only maps a
4-character code to the (kind, entity_id) pair a real domain module already
produced, and hands that pair back for the caller to resolve through the
actual domain module.

Alphabet excludes l, o, 0, 1 -- the glyphs people misread out of a chat
window -- matching `core/schema.sql`'s CHECK on `addresses.code`.
"""
from __future__ import annotations

import secrets
import sqlite3
from typing import Optional

from core.db import db_in

ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"  # no l, o, 0, 1
CODE_LEN = 4


def _new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def mint(kind: str, entity_id: str, *, conn: Optional[sqlite3.Connection] = None) -> str:
    """Return the address code for (kind, entity_id), minting one if this is
    the first time this entity has been addressed. `UNIQUE(kind, entity_id)`
    makes this idempotent -- calling it twice for the same order never mints
    a second code, and a code collision across entities just retries."""
    with db_in(conn) as c:
        row = c.execute(
            "SELECT code FROM addresses WHERE kind = ? AND entity_id = ?",
            (kind, str(entity_id)),
        ).fetchone()
        if row is not None:
            return row["code"]

        for _ in range(25):
            code = _new_code()
            try:
                c.execute(
                    "INSERT INTO addresses (code, kind, entity_id) VALUES (?, ?, ?)",
                    (code, kind, str(entity_id)),
                )
                return code
            except sqlite3.IntegrityError:
                continue  # code collision -- try another
        raise RuntimeError("could not mint a unique address code after 25 attempts")


def resolve(code: str, *, conn: Optional[sqlite3.Connection] = None) -> Optional[tuple[str, str]]:
    """Look up a typed code. Returns (kind, entity_id) or None. This is the
    ONE place in the whole surface a user is allowed to type a short code by
    hand -- CONTRACT.md section 1 names it as the alternative to a raw id."""
    code = (code or "").strip().lower()
    if len(code) != CODE_LEN:
        return None
    with db_in(conn) as c:
        row = c.execute(
            "SELECT kind, entity_id FROM addresses WHERE code = ?", (code,)
        ).fetchone()
    if row is None:
        return None
    return row["kind"], row["entity_id"]
