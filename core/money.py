"""The ledger. Every rule in here has a bug behind it.

Design in one paragraph: money is an INTEGER, always. A wallet row is the
balance; `ledger_entries` is the append-only history; `ledger_holds` is escrow.
Every movement is ONE `UPDATE ... WHERE <entire precondition>` whose success is
judged by `rowcount`, never a read-then-write, and never a clamp -- a system
that writes `MAX(0, coins - amount)` instead of failing is a system that quietly
pays out money it does not have.

Services are named and scoped. `games` may transfer and hold; it may not mint.
A betting bug can therefore misallocate money but can never create it.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .db import connection, db_in

# ------------------------------------------------------------------ services

MINT = "wallet.mint"
TRANSFER = "wallet.transfer"
HOLD = "hold.place"
FLAG = "wallet.flag"

# Who may do what. `games` deliberately has no MINT and no FLAG: it can neither
# create coins nor lift the restriction that governs it.
SERVICE_SCOPES: dict[str, frozenset[str]] = {
    "owner": frozenset({MINT, TRANSFER, HOLD, FLAG}),
    "shop": frozenset({TRANSFER, HOLD}),
    "games": frozenset({TRANSFER, HOLD}),
    "web": frozenset(),
    "public": frozenset(),   # the default: no authority whatsoever
}

SERVICE_TREASURY: dict[str, str] = {
    "shop": "treasury:shop",
    "games": "treasury:games",
    "owner": "treasury:house",
}

# What a human calls each treasury. Internal ids never appear in a surface a
# person looks at -- "treasury:shop" is plumbing, "Shop float" is the thing.
TREASURY_NAMES: dict[str, str] = {
    "treasury:shop": "Shop float",
    "treasury:games": "House bank",
    "treasury:house": "Owner reserve",
}


# Services whose holds are wagers, and so must respect the self-exclusion flag.
GAMBLING_SERVICES = frozenset({"games"})

WALLET_FLAGS = frozenset({"gambling_blocked", "orders_blocked", "staff"})

STALE_CLAIM_MINUTES = 15

# Below SQLite's 2**63-1 with room to spare. Past that limit the column is
# silently promoted to REAL, the balance rounds through a float while the
# ledger stays exact, and the conservation query raises "integer overflow".
MAX_COINS = 10 ** 15


class MoneyError(RuntimeError):
    """Base class. Every failure below is a refusal, never a partial apply."""


class InsufficientFunds(MoneyError): pass
class CeilingExceeded(MoneyError): pass
class WalletFrozen(MoneyError): pass
class NotPermitted(MoneyError): pass
class GamblingBlocked(MoneyError): pass
class IdempotencyConflict(MoneyError): pass
class IdempotencyInProgress(MoneyError): pass
class IdempotencyUnresolved(MoneyError): pass


@dataclass(frozen=True)
class Balance:
    subject: str
    coins: int
    held: int
    frozen: bool

    @property
    def available(self) -> int:
        return self.coins - self.held


@dataclass(frozen=True)
class Claim:
    key: str
    owned: bool
    replay: bool
    response: Any = None


# ------------------------------------------------------------------ helpers

def _int(value: object, name: str) -> int:
    # bool is an int subclass. It is not money.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    return value


def _positive(value: object, name: str) -> int:
    amount = _int(value, name)
    if amount <= 0:
        raise ValueError(f"{name} must be positive")
    return amount


def _require_scope(service: str, scope: str) -> None:
    if scope not in SERVICE_SCOPES.get(service, frozenset()):
        raise NotPermitted(f"service {service!r} may not {scope}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalise_subject(subject: str) -> str:
    """Canonical form of a wallet subject: lowercase, stripped.

    WHY THIS EXISTS -- the self-approval bypass via un-normalised identity.
    Identity guards in this codebase (e.g. `orders.approve`'s check that the
    approver never claimed/delivered the order) work by comparing one subject
    string to another with plain `==`. "U:99" and "u:99" are the same real
    subject, but they are not `==` to each other, so a worker who claims and
    delivers as "U:99" and later approves as "u:99" walks straight past a
    guard built on string equality -- the strings just never matched.

    Normalising HERE, at the boundary where a subject string is accepted, and
    storing the normalised form (not just normalising inside one comparison)
    means every later comparison anywhere -- this guard, or the next one
    written in a different module -- is automatically safe. Special-casing
    the fix inside a single `==` would leave every other comparison with the
    identical hole.
    """
    if not isinstance(subject, str):
        raise TypeError(f"subject must be a str, got {type(subject).__name__}")
    normalised = subject.strip().lower()
    if not normalised:
        raise ValueError("subject must not be empty")
    return normalised


def user(discord_id: int | str) -> str:
    """Wallet subject for a Discord user."""
    return f"u:{discord_id}"


def new_event_id(prefix: str) -> str:
    """A real per-event id, minted at the source.

    Never reconstruct an id from a timestamp: 'now minus N minutes' drifts
    between runs, so the same event dedups differently on a retry and pays
    twice.
    """
    return f"{prefix}:{secrets.token_hex(8)}"


_HELD = ("COALESCE((SELECT SUM(h.amount - h.captured - h.released) FROM ledger_holds h "
         "WHERE h.subject = wallets.subject AND h.state = 'open'), 0)")


# ------------------------------------------------------------------ wallets

def ensure_wallet(subject: str, deficit_floor: int = 0, *, service: str = "public",
                  conn: Optional[sqlite3.Connection] = None) -> None:
    """Create the wallet if it is missing.

    A NON-ZERO deficit floor is the authority to spend money that does not
    exist yet, which is minting wearing a different hat -- so it needs MINT
    scope. Without this check any service holding only TRANSFER could register
    a subject with a huge floor and transfer real, spendable coins out of it,
    which is exactly the guarantee this module claims to make about `games`.

    A floor given for a wallet that already exists is APPLIED, not dropped.
    `ON CONFLICT DO NOTHING` used to silence it, so a treasury auto-created by
    an earlier transfer kept floor 0 for ever and the real setup call was a
    silent no-op.
    """
    _int(deficit_floor, "deficit_floor")
    if deficit_floor < 0:
        raise ValueError("deficit_floor must not be negative")
    if deficit_floor:
        _require_scope(service, MINT)
    with db_in(conn) as c:
        c.execute(
            "INSERT INTO wallets (subject, deficit_floor) VALUES (?, ?) "
            "ON CONFLICT(subject) DO NOTHING",
            (subject, deficit_floor),
        )
        if deficit_floor:
            c.execute(
                "UPDATE wallets SET deficit_floor = ? WHERE subject = ? AND deficit_floor <> ?",
                (deficit_floor, subject, deficit_floor),
            )


def balance(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> Balance:
    """Read the balance. `held` is summed live from open holds, never cached."""
    with db_in(conn) as c:
        row = c.execute(
            f"SELECT subject, CAST(coins AS INTEGER) AS coins, frozen, {_HELD} AS held "
            "FROM wallets WHERE subject = ?",
            (subject,),
        ).fetchone()
    if row is None:
        return Balance(subject, 0, 0, False)
    return Balance(row["subject"], int(row["coins"]), int(row["held"]), bool(row["frozen"]))


def _apply(conn: sqlite3.Connection, subject: str, delta: int, *, service: str,
           reason: str, ref_kind: str | None, ref_id: str | None,
           idem_key: str | None) -> int:
    """Move `delta` coins and write the ledger entry, in one transaction.

    The audit entry and the balance write commit together. Not a best-effort
    side call afterwards -- a crash between the two leaves money that moved with
    no record of why.

    The guard is the whole precondition in one WHERE:
      - the wallet exists
      - it is not frozen
      - the result still clears (held - deficit_floor)

    A user's floor is 0, so they may spend down to their held coins and no
    further. Only a treasury has a non-zero floor, and that is the only way any
    subject may go negative.
    """
    _int(delta, "delta")
    if delta == 0:
        raise ValueError("delta must not be zero")
    if not reason or not reason.strip():
        raise ValueError("every ledger entry needs a reason")

    cur = conn.execute(
        f"UPDATE wallets SET coins = coins + :delta "
        f" WHERE subject = :subject "
        f"   AND frozen = 0 "
        f"   AND coins + :delta >= {_HELD} - deficit_floor "
        f"   AND coins + :delta <= :ceiling",
        {"delta": delta, "subject": subject, "ceiling": MAX_COINS},
    )
    if cur.rowcount != 1:
        # Distinguish the refusals for the caller, from state we now re-read.
        current = balance(subject, conn=conn)
        if current.frozen:
            raise WalletFrozen(f"{subject} is frozen")
        if delta > 0 and current.coins + delta > MAX_COINS:
            raise CeilingExceeded(
                f"{subject}: {current.coins + delta:,} would exceed the {MAX_COINS:,} ceiling"
            )
        raise InsufficientFunds(
            f"{subject}: needs {-delta:,}, has {current.available:,} available"
        )

    after = conn.execute(
        "SELECT CAST(coins AS INTEGER) AS coins FROM wallets WHERE subject = ?",
        (subject,),
    ).fetchone()["coins"]

    conn.execute(
        "INSERT INTO ledger_entries "
        "(subject, delta, balance_after, service, reason, ref_kind, ref_id, idem_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (subject, delta, int(after), service, reason, ref_kind, ref_id, idem_key),
    )
    return int(after)


def mint(subject: str, amount: int, *, service: str, reason: str,
         ref_kind: str | None = None, ref_id: str | None = None,
         idem_key: str | None = None,
         conn: Optional[sqlite3.Connection] = None) -> int:
    """Create coins. Only `owner` may do this."""
    _require_scope(service, MINT)
    amount = _positive(amount, "amount")
    with db_in(conn) as c:
        ensure_wallet(subject, service=service, conn=c)
        return _apply(c, subject, amount, service=service, reason=reason,
                      ref_kind=ref_kind, ref_id=ref_id, idem_key=idem_key)


def transfer(src: str, dst: str, amount: int, *, service: str, reason: str,
             ref_kind: str | None = None, ref_id: str | None = None,
             idem_key: str | None = None,
             conn: Optional[sqlite3.Connection] = None) -> None:
    """Move coins between two subjects. Both legs commit or neither does."""
    _require_scope(service, TRANSFER)
    amount = _positive(amount, "amount")
    if src == dst:
        raise ValueError("cannot transfer to self")
    with db_in(conn) as c:
        ensure_wallet(src, service=service, conn=c)
        ensure_wallet(dst, service=service, conn=c)
        _apply(c, src, -amount, service=service, reason=reason,
               ref_kind=ref_kind, ref_id=ref_id, idem_key=idem_key)
        _apply(c, dst, amount, service=service, reason=reason,
               ref_kind=ref_kind, ref_id=ref_id, idem_key=idem_key)


# ------------------------------------------------------------------ holds

def _open_hold(conn: sqlite3.Connection, hold_id: str) -> sqlite3.Row:
    """Fetch a hold that is still open, or refuse loudly.

    An exhausted hold used to return 0 here and the caller could not tell that
    from a successful zero-value operation. A silent no-op on a money path is
    how a pipeline gets poisoned: the caller believes it settled a bet it never
    touched. Callers that need retry-safety take an idempotency claim; they do
    not get it from a quiet zero.
    """
    row = conn.execute("SELECT * FROM ledger_holds WHERE id = ?", (hold_id,)).fetchone()
    if row is None:
        raise MoneyError(f"no such hold {hold_id!r}")
    if row["state"] != "open":
        raise MoneyError(f"hold {hold_id} is {row['state']}, not open")
    remaining = int(row["amount"]) - int(row["captured"]) - int(row["released"])
    if remaining <= 0:
        raise MoneyError(f"hold {hold_id} has nothing left")
    return row


def place_hold(subject: str, amount: int, *, service: str, reason: str,
               hold_id: str | None = None, expires_in_minutes: int | None = None,
               conn: Optional[sqlite3.Connection] = None) -> str:
    """Escrow `amount` against `subject`. Coins do not move; availability drops.

    This is the first money-committing step of a bet, which is why the
    self-exclusion flag is enforced HERE and not at capture. By capture time the
    player has already been allowed to play.

    The whole precondition -- wallet exists, not frozen, enough available, not
    gambling-blocked when the service is a wagering one -- is inside one
    INSERT ... SELECT ... WHERE, so a concurrent second hold cannot slip past a
    check that already passed.
    """
    _require_scope(service, HOLD)
    amount = _positive(amount, "amount")
    hold_id = hold_id or new_event_id("hold")
    gate = 1 if service in GAMBLING_SERVICES else 0
    expires = None
    if expires_in_minutes:
        expires = (datetime.now(timezone.utc)
                   + timedelta(minutes=expires_in_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    with db_in(conn) as c:
        ensure_wallet(subject, service=service, conn=c)
        cur = c.execute(
            f"INSERT INTO ledger_holds (id, subject, amount, service, reason, expires_at) "
            f"SELECT :hid, :subject, :amount, :service, :reason, :expires "
            f"  FROM wallets "
            f" WHERE wallets.subject = :subject "
            f"   AND wallets.frozen = 0 "
            f"   AND wallets.coins - {_HELD} >= :amount "
            f"   AND (:gate = 0 OR NOT EXISTS (SELECT 1 FROM wallet_flags f "
            f"        WHERE f.subject = :subject AND f.flag = 'gambling_blocked'))",
            {"hid": hold_id, "subject": subject, "amount": amount, "service": service,
             "reason": reason, "expires": expires, "gate": gate},
        )
        if cur.rowcount != 1:
            current = balance(subject, conn=c)
            if current.frozen:
                raise WalletFrozen(f"{subject} is frozen")
            if gate and _has_flag(c, subject, "gambling_blocked"):
                raise GamblingBlocked(f"{subject} has self-excluded")
            raise InsufficientFunds(
                f"{subject}: needs {amount:,}, has {current.available:,} available"
            )
    return hold_id


def capture_hold(hold_id: str, amount: int | None = None, *, service: str,
                 reason: str, to: str | None = None,
                 ref_kind: str | None = None, ref_id: str | None = None,
                 idem_key: str | None = None,
                 conn: Optional[sqlite3.Connection] = None) -> int:
    """Spend escrowed coins. Optionally credit `to` with the same amount.

    Order matters: the hold shrinks FIRST, which raises the subject's available
    balance by exactly the captured amount, and only then does the debit run
    through the same guard every other debit uses. One guard, no special case.
    """
    with db_in(conn) as c:
        hold = _open_hold(c, hold_id)
        remaining = int(hold["amount"]) - int(hold["captured"]) - int(hold["released"])
        take = remaining if amount is None else _positive(amount, "amount")
        if take > remaining:
            raise MoneyError(f"hold {hold_id} has {remaining:,} left, cannot capture {take:,}")

        cur = c.execute(
            "UPDATE ledger_holds "
            "   SET captured = captured + :take, "
            "       state = CASE WHEN captured + released + :take >= amount "
            "                    THEN 'captured' ELSE state END "
            " WHERE id = :hid AND state = 'open' "
            "   AND captured + released + :take <= amount",
            {"take": take, "hid": hold_id},
        )
        if cur.rowcount != 1:
            raise MoneyError(f"hold {hold_id} was not open for capture")

        _apply(c, hold["subject"], -take, service=service, reason=reason,
               ref_kind=ref_kind, ref_id=ref_id, idem_key=idem_key)
        if to:
            ensure_wallet(to, service=service, conn=c)
            _apply(c, to, take, service=service, reason=reason,
                   ref_kind=ref_kind, ref_id=ref_id, idem_key=idem_key)
    return take


def release_hold(hold_id: str, amount: int | None = None, *,
                 conn: Optional[sqlite3.Connection] = None) -> int:
    """Give escrowed coins back. Availability rises; no coins move."""
    with db_in(conn) as c:
        hold = _open_hold(c, hold_id)
        remaining = int(hold["amount"]) - int(hold["captured"]) - int(hold["released"])
        give = remaining if amount is None else _positive(amount, "amount")
        if give > remaining:
            raise MoneyError(f"hold {hold_id} has {remaining:,} left, cannot release {give:,}")
        cur = c.execute(
            "UPDATE ledger_holds "
            "   SET released = released + :give, "
            "       state = CASE WHEN captured + released + :give >= amount "
            "                    THEN 'released' ELSE state END "
            " WHERE id = :hid AND state = 'open' "
            "   AND captured + released + :give <= amount",
            {"give": give, "hid": hold_id},
        )
        if cur.rowcount != 1:
            raise MoneyError(f"hold {hold_id} was not open for release")
    return give


# ------------------------------------------------------------------ flags

def _has_flag(conn: sqlite3.Connection, subject: str, flag: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM wallet_flags WHERE subject = ? AND flag = ?", (subject, flag)
    ).fetchone()
    return row is not None


def set_flag(subject: str, flag: str, *, service: str, set_by: str,
             note: str | None = None,
             conn: Optional[sqlite3.Connection] = None) -> None:
    """Set a wallet flag. Only `owner` may -- notably, `games` may not set or
    clear `gambling_blocked`, so the service a restriction governs can never
    lift it."""
    _require_scope(service, FLAG)
    if flag not in WALLET_FLAGS:
        raise ValueError(f"unknown flag {flag!r}")
    with db_in(conn) as c:
        ensure_wallet(subject, service=service, conn=c)
        c.execute(
            "INSERT INTO wallet_flags (subject, flag, set_by, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subject, flag) DO UPDATE SET set_by = excluded.set_by, "
            "note = excluded.note, set_at = datetime('now')",
            (subject, flag, set_by, note),
        )


def clear_flag(subject: str, flag: str, *, service: str,
               conn: Optional[sqlite3.Connection] = None) -> None:
    _require_scope(service, FLAG)
    with db_in(conn) as c:
        c.execute("DELETE FROM wallet_flags WHERE subject = ? AND flag = ?", (subject, flag))


def flags(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> set[str]:
    with db_in(conn) as c:
        rows = c.execute("SELECT flag FROM wallet_flags WHERE subject = ?", (subject,)).fetchall()
    return {r["flag"] for r in rows}


# ------------------------------------------------------------------ idempotency

def fingerprint(payload: Any) -> str:
    """Stable hash of a request body. Key collisions with a different payload
    are a loud conflict, never a silent overwrite."""
    # No `default=` fallback on purpose. Coercing unknown types with str()
    # made Decimal("100") and "100" hash the same, so a different request
    # replayed as an idempotent match. An unserialisable payload is a caller
    # bug and should say so here, loudly, rather than at settlement time.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim(key: str, *, service: str, endpoint: str, payload: Any,
          conn: sqlite3.Connection) -> Claim:
    """Claim the right to perform `key` exactly once.

    `conn` is REQUIRED, and it must be a transaction that also carries the work
    and the `complete()`. Prefer `guarded()`, which arranges exactly that.

    The parameter is mandatory because the optional version was a trap: called
    on its own it committed the claim in its own transaction, so a crash after
    the money moved but before `complete()` left an 'in_progress' row that went
    stale, was re-owned, and paid a second time. Sharing one transaction means
    a crash takes the claim down with the money.

    VALIDATE THE REQUEST FULLY BEFORE CALLING THIS. If a claim is taken and the
    work then fails validation, a concurrent retry gets a success-shaped replay
    for a request that would have been refused.

    Returns a Claim: `owned` means go ahead, `replay` means it was already done
    and `response` holds what was returned the first time.
    """
    now = _now()
    stale = (datetime.now(timezone.utc)
             - timedelta(minutes=STALE_CLAIM_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    ph = fingerprint(payload)

    with db_in(conn) as c:
        cur = c.execute(
            "INSERT INTO idempotency "
            "(key, service, endpoint, payload_hash, state, applied_unknown, created_at, updated_at) "
            "VALUES (:key, :svc, :ep, :ph, 'in_progress', 0, :now, :now) "
            "ON CONFLICT(key) DO UPDATE SET created_at = :now, updated_at = :now "
            " WHERE idempotency.state = 'in_progress' "
            "   AND idempotency.applied_unknown = 0 "
            "   AND idempotency.created_at < :stale "
            "   AND idempotency.service = :svc "
            "   AND idempotency.endpoint = :ep "
            "   AND idempotency.payload_hash = :ph",
            {"key": key, "svc": service, "ep": endpoint, "ph": ph, "now": now, "stale": stale},
        )
        if cur.rowcount == 1:
            return Claim(key, owned=True, replay=False)

        row = c.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()

    if row is None:                                   # lost a race; treat as busy
        raise IdempotencyInProgress(key)
    if row["service"] != service or row["endpoint"] != endpoint or row["payload_hash"] != ph:
        raise IdempotencyConflict(f"{key} was used for a different request")
    if row["applied_unknown"]:
        raise IdempotencyUnresolved(f"{key} completed out of band; resolve by hand")
    if row["state"] == "done":
        response = json.loads(row["response_json"]) if row["response_json"] else None
        return Claim(key, owned=False, replay=True, response=response)
    if row["state"] == "failed":
        raise MoneyError(f"{key} previously failed")
    raise IdempotencyInProgress(key)


def complete(key: str, response: Any = None, *,
             conn: sqlite3.Connection) -> None:
    """Mark a claim done. Must run in the SAME transaction that claimed it."""
    with db_in(conn) as c:
        c.execute(
            "UPDATE idempotency SET state = 'done', response_json = ?, updated_at = ? "
            " WHERE key = ? AND state = 'in_progress'",
            (json.dumps(response, default=str) if response is not None else None, _now(), key),
        )


def fail(key: str, *, applied_unknown: bool = False,
         conn: sqlite3.Connection) -> None:
    """Release a claim after a failure.

    `applied_unknown=True` when the effect may have happened outside our
    transaction (a Discord call that timed out, say). Such a claim is never
    auto-retried and never auto-cleaned -- a human resolves it.
    """
    with db_in(conn) as c:
        c.execute(
            "UPDATE idempotency SET state = 'failed', applied_unknown = ?, updated_at = ? "
            " WHERE key = ? AND state = 'in_progress'",
            (1 if applied_unknown else 0, _now(), key),
        )


# Ledger kinds that are wagering activity. The website may never show these:
# wagering is Discord-only by the owner's explicit decision (CONTRACT.md 1, 9).
WAGERING_REF_KINDS: frozenset[str] = frozenset({"game_round", "pred_market"})


def public_history(subject: str, limit: int = 50, *,
                   conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Ledger rows that may be shown on the PUBLIC website.

    The website must never surface Discord-only activity (CONTRACT.md 1, 9).
    That decision lives here, in core, not in a page: a caller asks for the
    public view and gets it, so a new page cannot leak by forgetting a filter,
    and the excluded set has exactly one definition.
    """
    return history(subject, limit=limit,
                   exclude_ref_kinds=WAGERING_REF_KINDS, conn=conn)


def history(subject: str, limit: int = 50, *,
            exclude_ref_kinds: Iterable[str] | None = None,
            conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """Recent ledger entries, newest first.

    `exclude_ref_kinds` filters IN THE QUERY on purpose. The website was
    fetching ten times the rows it needed and dropping the wagering ones in
    Python afterwards; a boundary enforced after the fetch is one the next
    page to forget the filter walks straight through, and it silently
    returns fewer rows than `limit` asked for.
    """
    excluded = list(exclude_ref_kinds or ())
    sql = ("SELECT ts, delta, balance_after, reason, service, ref_kind, ref_id "
           "  FROM ledger_entries WHERE subject = ?")
    params: list[Any] = [subject]
    if excluded:
        holes = ",".join("?" * len(excluded))
        # IS NULL survives the NOT IN, which would otherwise drop unreffed rows.
        sql += f" AND (ref_kind IS NULL OR ref_kind NOT IN ({holes}))"
        params.extend(excluded)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db_in(conn) as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@dataclass
class Guard:
    """Handle yielded by `guarded()`. `conn` is the ONE transaction."""
    claim: Claim
    conn: sqlite3.Connection
    response: Any = None

    @property
    def replay(self) -> bool:
        return self.claim.replay

    def set_response(self, value: Any) -> None:
        """What a later replay of this key will return. Keep it small."""
        self.response = value


@contextmanager
def guarded(key: str, *, service: str, endpoint: str, payload: Any):
    """Claim, do the work, and record completion -- in ONE transaction.

    Use this instead of calling `claim()` / `complete()` by hand. Doing those
    as separate transactions leaves a window between the money moving and the
    claim being marked done; a crash inside that window strands an
    'in_progress' row that later goes stale, gets reclaimed, and pays twice.

        with money.guarded(key, service="shop", endpoint="payout",
                           payload=body) as g:
            if g.replay:
                return g.response
            money.transfer(..., conn=g.conn, idem_key=key)
            g.set_response({"paid": amount})

    Anything raised inside the block rolls the whole thing back, claim
    included, and the key is free to be retried cleanly.
    """
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        c = claim(key, service=service, endpoint=endpoint, payload=payload, conn=conn)
        # On a replay the caller wants the ORIGINAL response, not an empty one.
        guard = Guard(claim=c, conn=conn, response=c.response if c.replay else None)
        yield guard
        if not c.replay:
            complete(key, guard.response, conn=conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def bootstrap_treasuries(*, games_floor: int = 0, house_floor: int = 0,
                         conn: Optional[sqlite3.Connection] = None) -> None:
    """Create the treasury wallets. Owner-scoped, called once at boot.

    `games_floor` is how far `treasury:games` may go negative -- how much the
    casino may owe beyond what it has collected. ZERO is the safe default: the
    house simply refuses a payout it cannot fund, rather than inventing coins.
    Raise it deliberately, knowing it is a licence to run a deficit.
    """
    ensure_wallet("treasury:shop", 0, service="owner", conn=conn)
    ensure_wallet("treasury:games", games_floor, service="owner", conn=conn)
    ensure_wallet("treasury:house", house_floor, service="owner", conn=conn)
