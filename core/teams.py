"""core/teams.py -- manager-led worker teams.

Ported from AbexTech's `cogs/team.py` / `views/team_settings.py`, roster and
naming only -- no money, no manager override commission on order payouts,
no in-game-name linking. AbexTech's version exists to attribute CSN/chest-
shop sales back to a person; this shop has no such external sales feed, so
there is nothing for an IGN to link, and no per-order cut to compute. See
CONTRACT.md section 11d.

Nothing here moves a coin, so unlike land/bonds/loans this module never
opens `money.guarded` -- a team join/leave/rename is not an event that can
be replayed into a double charge, just a row that can be re-written safely.

One team per manager (`teams.manager` is UNIQUE) -- a manager who wants a
new name renames their team, they don't get a second one. One team per
member at a time (`team_members.subject` is UNIQUE) -- joining a new team
silently leaves whichever one a member was already on, same "last write
wins" shape as `loyalty_overrides`.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import money
from .db import db_in


class TeamError(RuntimeError):
    """Base class. A refusal, never a partial apply."""


class AlreadyHasTeam(TeamError):
    """This manager already runs a team -- rename it instead of making a second."""


class UnknownTeam(TeamError):
    pass


class NotYourTeam(TeamError):
    """The caller isn't this team's manager -- roster edits and rename/disband
    are manager-only, checked here so no caller has to remember to."""


def _team_row(c: sqlite3.Connection, team_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if row is None:
        raise UnknownTeam(team_id)
    return row


def _require_manager_by_lookup(c: sqlite3.Connection, manager: str) -> sqlite3.Row:
    row = c.execute("SELECT * FROM teams WHERE manager = ?", (manager,)).fetchone()
    if row is None:
        raise UnknownTeam(manager)
    return row


# ------------------------------------------------------------------ manager actions

def create(manager: str, name: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    """A manager stands up their team. Refuses a second team for the same
    manager rather than silently renaming an existing one -- `rename`
    exists for that, explicitly, so a manager can never lose their roster
    by mis-clicking "create" twice."""
    name = (name or "").strip()[:40] or "Unnamed team"
    with db_in(conn) as c:
        money.ensure_wallet(manager, conn=c)
        existing = c.execute("SELECT id FROM teams WHERE manager = ?", (manager,)).fetchone()
        if existing is not None:
            raise AlreadyHasTeam(existing["id"])
        cur = c.execute(
            "INSERT INTO teams (manager, name) VALUES (?, ?)", (manager, name)
        )
        return int(cur.lastrowid)


def rename(manager: str, name: str, *, conn: Optional[sqlite3.Connection] = None) -> None:
    name = (name or "").strip()[:40] or "Unnamed team"
    with db_in(conn) as c:
        row = c.execute("SELECT id FROM teams WHERE manager = ?", (manager,)).fetchone()
        if row is None:
            raise UnknownTeam(manager)
        c.execute("UPDATE teams SET name = ? WHERE id = ?", (name, row["id"]))


def disband(manager: str, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Deletes the team and every membership row with it -- `ON DELETE
    CASCADE` does the membership half; nothing here needs to enumerate the
    roster first."""
    with db_in(conn) as c:
        row = c.execute("SELECT id FROM teams WHERE manager = ?", (manager,)).fetchone()
        if row is None:
            raise UnknownTeam(manager)
        c.execute("DELETE FROM teams WHERE id = ?", (row["id"],))


def add_member(manager: str, subject: str, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Manager-driven add -- the counterpart to a worker self-joining via
    `join`. Both end at the same `INSERT OR REPLACE`, so it makes no
    difference which side initiated it; the result is one row either way."""
    with db_in(conn) as c:
        team = _require_manager_by_lookup(c, manager)
        if subject == manager:
            raise TeamError("a manager can't join their own team as a member")
        money.ensure_wallet(subject, conn=c)
        c.execute(
            "INSERT OR REPLACE INTO team_members (team_id, subject) VALUES (?, ?)",
            (team["id"], subject),
        )


def remove_member(manager: str, subject: str, *, conn: Optional[sqlite3.Connection] = None) -> None:
    with db_in(conn) as c:
        team = _require_manager_by_lookup(c, manager)
        c.execute(
            "DELETE FROM team_members WHERE team_id = ? AND subject = ?",
            (team["id"], subject),
        )


# ------------------------------------------------------------------ worker actions

def join(subject: str, team_id: int, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Self-serve join. Leaves whichever team `subject` was already on --
    `team_members.subject` is UNIQUE, so `INSERT OR REPLACE` is exactly
    "move me to this team", never a second row."""
    with db_in(conn) as c:
        team = _team_row(c, team_id)
        if subject == team["manager"]:
            raise TeamError("a manager can't join their own team as a member")
        money.ensure_wallet(subject, conn=c)
        c.execute(
            "INSERT OR REPLACE INTO team_members (team_id, subject) VALUES (?, ?)",
            (team_id, subject),
        )


def leave(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> bool:
    """True if `subject` was on a team and is now off it; False if they were
    never on one -- so a caller can tell "left" from "nothing to leave"."""
    with db_in(conn) as c:
        cur = c.execute("DELETE FROM team_members WHERE subject = ?", (subject,))
        return cur.rowcount > 0


# ------------------------------------------------------------------ reads

def team_of(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> Optional[sqlite3.Row]:
    """The team `subject` manages, or the team they're a member of --
    whichever applies. A subject is never both, since `add_member`/`join`
    refuse a manager joining their own team."""
    with db_in(conn) as c:
        row = c.execute("SELECT * FROM teams WHERE manager = ?", (subject,)).fetchone()
        if row is not None:
            return row
        return c.execute(
            "SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id "
            "WHERE m.subject = ?",
            (subject,),
        ).fetchone()


def roster(team_id: int, *, conn: Optional[sqlite3.Connection] = None) -> list[str]:
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT subject FROM team_members WHERE team_id = ? ORDER BY joined_at ASC",
            (team_id,),
        ).fetchall()
    return [r["subject"] for r in rows]


def list_teams(*, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Every team with its member count, most recently created first --
    used by the join picker and by anyone just browsing what exists."""
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT t.id, t.manager, t.name, t.created_at, "
            "       (SELECT COUNT(*) FROM team_members m WHERE m.team_id = t.id) AS member_count "
            "  FROM teams t ORDER BY t.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
