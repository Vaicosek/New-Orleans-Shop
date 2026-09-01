"""`/teams` -- the public directory of the shop's manager-led teams.

Read-only, like the storefront and the market pages: it opens the local
database and nothing else, so it answers whether or not the Discord bot is
running. Teams are roster-and-naming only (CONTRACT.md section 11d), so
there is no money on this page at all -- a team holds no wallet of its own.

This is the VIEW, not the action. Creating a team, joining, leaving,
renaming and every roster edit live on Discord's `/team` command, exactly
the way bidding lives on the Discord card. So there is no form here, and no
signed-in variant of the page -- just a directory, and one line saying where
the actions are.

Reads go through `core.teams.list_teams()` / `core.teams.roster()` rather
than hand-written SELECTs, so the site can never disagree with what the
Discord command shows. Nothing under `bot/` is imported: `web/` is a
separate process with no gateway connection, and the section 9 import wall
scans this directory.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from core import teams as core_teams

from ..auth import resolve_identity
from ..shell import esc, page


def _short_id(subject: str) -> str:
    """`u:` is internal database vocabulary, not English, so it is stripped
    for display. This process holds no gateway connection and cannot resolve
    a Discord id into a display name, so it shows the bare id rather than
    inventing one."""
    if isinstance(subject, str) and subject.startswith("u:"):
        return subject.split(":", 1)[1]
    return str(subject)


def _formed_text(created_at: object) -> str:
    """`teams.created_at` is a naive UTC string ("%Y-%m-%d %H:%M:%S").
    Stamp it UTC explicitly before it is read as a time, or a naive
    datetime reads as local time and the date can land a day out. Rendered
    in full -- "3 August 2026", never a truncated or numeric-only stamp."""
    if not created_at:
        return ""
    try:
        dt = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return str(created_at)
    return f"{dt.day} {dt.strftime('%B')} {dt.year}"


def _member_count_text(count: int) -> str:
    return "1 member" if count == 1 else f"{count:,} members"


def _sorted_teams(rows: list[dict]) -> list[dict]:
    """`list_teams()` hands back insertion order (newest first), which is
    useful to a join picker and useless to a reader. A directory is read
    largest-first -- the busiest teams are the ones a visitor is looking
    for -- with names alphabetical inside each size so the order is stable
    and a team never appears to move between refreshes."""
    return sorted(
        rows,
        key=lambda t: (-int(t.get("member_count") or 0), str(t.get("name") or "").lower()),
    )


def _team_block(team: dict, members: list[str], roster_readable: bool) -> str:
    count = int(team.get("member_count") or 0)
    manager = _short_id(team.get("manager"))
    formed = _formed_text(team.get("created_at"))

    if not roster_readable:
        # The team row read fine but its roster did not. "We could not read
        # it" and "it is empty" are different facts and must not look alike.
        names = '<span class="dim">Roster could not be read just now.</span>'
    elif members:
        names = ", ".join(esc(_short_id(m)) for m in members)
    else:
        names = '<span class="dim">No members yet.</span>'

    formed_row = (
        f'<div class="row"><span>Formed</span><span>{esc(formed)}</span></div>'
        if formed else ""
    )
    return f"""<h2>{esc(team.get("name") or "Unnamed team")}</h2>
<div class="sums">
<div class="row"><span>Manager</span><span>{esc(manager)}</span></div>
<div class="row"><span>Size</span><span>{esc(_member_count_text(count))}</span></div>
<div class="row"><span>Roster</span><span>{names}</span></div>
{formed_row}
</div>"""


async def teams(request: web.Request) -> web.Response:
    identity = await resolve_identity(request)

    read_failed = False
    rows: list[dict] = []
    try:
        rows = core_teams.list_teams()
    except Exception:  # noqa: BLE001 -- the page still renders without the list
        read_failed = True

    if read_failed:
        listing = ('<p class="notice">The team directory could not be read just now. '
                   'Nothing has changed &mdash; try again in a moment.</p>')
        summary = ""
    elif not rows:
        listing = '<p class="empty">No teams yet.</p>'
        summary = ""
    else:
        ordered = _sorted_teams(rows)
        blocks = []
        for team in ordered:
            members: list[str] = []
            roster_readable = True
            try:
                members = core_teams.roster(int(team["id"]))
            except Exception:  # noqa: BLE001 -- one bad roster, not a dead page
                roster_readable = False
            blocks.append(_team_block(team, members, roster_readable))
        listing = "".join(blocks)
        total_members = sum(int(t.get("member_count") or 0) for t in ordered)
        teams_word = "1 team" if len(ordered) == 1 else f"{len(ordered):,} teams"
        summary = (f'<p class="dim">{esc(teams_word)}, '
                   f'{esc(_member_count_text(total_members))} between them.</p>')

    body = f"""
<div class="hero">
<h1>Teams</h1>
<p>Every team working out of New Orleans, who manages it and who is on it.
Joining, leaving and roster changes happen on the <code>/team</code> command in
Discord &mdash; this page only shows what is there.</p>
</div>
{summary}
{listing}
"""
    return page("Teams", "teams", body, identity=identity)


def register(app: web.Application) -> None:
    app.router.add_get("/teams", teams)
