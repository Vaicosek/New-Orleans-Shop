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


def override_earned(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> int:
    """Total override this subject has been paid for managing a team.

    Read off `team_overrides` rows -- money that actually moved -- rather
    than recomputed from a percentage, so a period when the treasury could
    only part-fund an override counts what was really paid.
    """
    with db_in(conn) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(coins), 0) AS total FROM team_overrides WHERE manager = ?",
            (subject,),
        ).fetchone()
    return int(row["total"] or 0)


#: The manager's override on a team member's order payout, as a percent.
#: Carried over from AbexTech's MANAGER_OVERRIDE_ORDER_PCT default. The
#: COMPANY pays this -- see `manager_of`'s note and CONTRACT.md 11d.
MANAGER_OVERRIDE_PCT = 5


def manager_of(subject: str, *, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """The manager who earns an override on `subject`'s work, or None.

    Deliberately NOT `team_of(...)["manager"]`: that returns the team a
    subject MANAGES before the one they belong to, so a manager claiming an
    order themselves would come back as their own manager and be paid an
    override on their own payout. Only membership earns somebody else an
    override, so this reads `team_members` alone.
    """
    with db_in(conn) as c:
        row = c.execute(
            "SELECT t.manager FROM teams t JOIN team_members m ON m.team_id = t.id "
            " WHERE m.subject = ?",
            (subject,),
        ).fetchone()
    if row is None or row["manager"] == subject:
        return None
    return row["manager"]


# ------------------------------------------------------------------ focus

def set_focus(manager: str, categories: list[str], *,
              conn: Optional[sqlite3.Connection] = None) -> None:
    """Replace a team's focus outright with `categories`.

    Replace rather than merge: the panel that calls this hands over the
    complete set the manager selected, so treating it as an addition would
    make deselecting a category impossible -- the classic multi-select bug
    where the list only ever grows.

    An empty list clears the focus, which means the team works EVERYTHING
    again (see the schema note); it does not mean the team works nothing.
    """
    with db_in(conn) as c:
        team = _require_manager_by_lookup(c, manager)
        c.execute("DELETE FROM team_focus WHERE team_id = ?", (team["id"],))
        for category in dict.fromkeys(categories):   # de-duped, order kept
            c.execute(
                "INSERT OR IGNORE INTO team_focus (team_id, category) VALUES (?, ?)",
                (team["id"], category),
            )


def focus(team_id: int, *, conn: Optional[sqlite3.Connection] = None) -> list[str]:
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT category FROM team_focus WHERE team_id = ? ORDER BY category",
            (team_id,),
        ).fetchall()
    return [r["category"] for r in rows]


def teams_for_category(category: str, *,
                       conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Every team that would want an order in `category` -- the ones that
    named it, plus the ones that have named nothing and therefore work
    everything. This is what turns "ping the whole server for every order"
    into "ping the people who actually do this".

    A team with no members is excluded: pinging a roster of nobody is a
    mention that reaches nobody and makes the card noisier for everyone
    else.
    """
    with db_in(conn) as c:
        rows = c.execute(
            "SELECT t.id, t.manager, t.name, "
            "       (SELECT COUNT(*) FROM team_members m WHERE m.team_id = t.id) AS member_count "
            "  FROM teams t "
            " WHERE (EXISTS (SELECT 1 FROM team_focus f WHERE f.team_id = t.id "
            "                  AND f.category = ?) "
            "     OR NOT EXISTS (SELECT 1 FROM team_focus f WHERE f.team_id = t.id)) "
            "   AND EXISTS (SELECT 1 FROM team_members m WHERE m.team_id = t.id) "
            " ORDER BY t.name",
            (category,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ standings

def leaderboard(*, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Teams ranked by what their members have actually been PAID for
    completed work, highest first.

    Derived live from `order_claims` on every read -- never a running total
    on the team row. Same discipline as `core/loyalty.py`'s score and
    `core/wagering.py`'s exposure, and for the same reason: a stored
    counter and the ledger disagree the first time anything is voided,
    repriced or written off, and the counter is always the one that is
    wrong.

    `paid_coins` is the total actually paid for a claim, bonus included, so
    a team's standing reflects real money that left the treasury rather
    than a priced estimate of work. Only claims with a `paid_event` count:
    delivered-but-unapproved work is not yet money and must not rank.

    A team's MANAGER is counted alongside its members. In the system this
    is modelled on, the manager claims orders and hands them to their
    people, so leaving the manager's own claims out understates exactly
    the teams that are working hardest.

    Two figures make up a standing, and both are returned separately as
    well as summed: `worked` is gold paid for claims the team's people
    delivered, `managed` is override earned by running the team. A team
    that produces nothing and rides on overrides is visibly different from
    one that produces, and collapsing them into a single number would hide
    exactly that.
    """
    with db_in(conn) as c:
        rows = c.execute(
            "WITH roster AS ( "
            "  SELECT id AS team_id, manager AS subject FROM teams "
            "  UNION ALL "
            "  SELECT team_id, subject FROM team_members "
            ") "
            "SELECT t.id, t.name, t.manager, "
            "       (SELECT COUNT(*) FROM team_members m WHERE m.team_id = t.id) AS member_count, "
            "       COUNT(DISTINCT oc.order_id) AS orders, "
            "       COALESCE(SUM(oc.paid_coins), 0) AS worked, "
            "       (SELECT COALESCE(SUM(o.coins), 0) FROM team_overrides o "
            "         WHERE o.manager = t.manager) AS managed, "
            "       COALESCE(SUM(oc.paid_coins), 0) "
            "         + (SELECT COALESCE(SUM(o.coins), 0) FROM team_overrides o "
            "             WHERE o.manager = t.manager) AS paid "
            "  FROM teams t "
            "  JOIN roster r ON r.team_id = t.id "
            "  LEFT JOIN order_claims oc "
            "    ON oc.worker = r.subject AND oc.paid_event IS NOT NULL "
            " GROUP BY t.id, t.name, t.manager "
            " ORDER BY paid DESC, orders DESC, t.name ASC"
        ).fetchall()
    return [dict(r) for r in rows]


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
