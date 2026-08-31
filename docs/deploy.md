# Deploy — Wispbyte

Panel only. No shell, ever. Everything the server does is either a panel field or
the startup command; nothing on the box can be run by hand. That constraint is why
`db.seed_if_empty()` and the boot self-check exist at all.

## Panel configuration

| Field | Value |
|---|---|
| Docker image | Python 3.14 |
| Git repo address | this repo |
| Git branch | `main` |
| Auto update | on |
| App py file | irrelevant — the startup command below hardcodes the entrypoint |
| Startup command | below |

### Startup command

The egg's stock command is not sufficient. Use:

    if [[ ! -f /home/container/run_all.py ]]; then git -C /home/container remote add origin <repo url> 2>/dev/null; git -C /home/container fetch origin main && git -C /home/container checkout -B main origin/main; fi; if [[ -d .git ]]; then git pull --ff-only || true; fi; if [[ -f /home/container/requirements.txt ]]; then pip install -U --prefix .local -r requirements.txt; fi; /usr/local/bin/python /home/container/run_all.py

Three deliberate differences from the stock command:

1. **It repairs a `.git` that was never cloned into** (see below). The guard only
   fires when `run_all.py` is absent, so a healthy boot just pulls.
2. **It forces the local branch onto `main`.** A `git init` skeleton sits on
   `master`; the repo's branch is `main`, so a pull has nothing to reconcile and
   exits 0 having done nothing.
3. **It hardcodes `run_all.py` and `requirements.txt`** instead of `${PY_FILE}` and
   `${REQUIREMENTS_FILE}`. A blank panel variable in either turns into a silent
   no-op: `-f /home/container/` is a directory test that fails, so pip installs
   nothing and the bot boots without `discord.py`.

## The empty-skeleton failure

**Symptom.** `/home/container` holds `.git` and nothing else. Every boot is silent.
`git pull` reports success.

**Diagnosis from the Files view alone**, without a shell:

| Evidence | Means |
|---|---|
| no `index` file | nothing was ever checked out |
| no `logs/` directory | no ref was ever updated — no commit ever landed |
| `FETCH_HEAD` 0 bytes | the fetch ran and returned nothing |
| `HEAD` is 23 bytes | `ref: refs/heads/master` — a `git init` default, not a clone |
| `branches/` present, `packed-refs` absent | exactly what `git init` leaves behind |

**Cause.** The egg's install step branches on `if [ -d .git ]` — it pulls when it
finds one and clones only when it does not. A skeleton left behind by a failed
clone is therefore never repaired by a Reinstall: the pull succeeds, fetches
nothing, and exits 0. Deleting `.git` and reinstalling also fixes it; the startup
command above fixes it without either.

## Environment variables

**Names only.** No token, secret, id or webhook is ever written into this repo or
the brain — CONTRACT.md §11. The annotated list, with what each one does and how to
generate it, is `.env.example`.

### Where the values actually go

The panel's Python egg exposes a fixed set of variables — there is no field
to put `DISCORD_TOKEN` in, and no shell to export it from. So configuration
is a **`.env` file in the project root**, created with the panel's file
manager (Files → New File → `.env`), and read at startup by `core/env.py`.
No dotenv dependency: it is forty lines of `KEY=VALUE` parsing.

`.env` is gitignored and stays that way — which is also the answer to "why
isn't there one in the repo". `.env.example` is the annotated template.

**A variable already present in the real environment always wins over the
file.** The file fills gaps; it never overrides the host. That way a value
the panel injects cannot be silently shadowed by a stale line in a file
nobody remembered was there — and the loader prints the name of anything it
skipped for that reason, because "I set it in the file and nothing changed"
is otherwise an unfindable half-hour.

### The channels are built, not configured

`/setup`, run once in the server by its owner, creates the categories, channels and
roles and records their ids in `guild_layout`. Nothing needs pasting into `.env`.

It previews first — how many will be created, adopted and skipped — and it is safe to
run twice: a second run creates nothing, and a channel someone already made by hand
with a matching name is **adopted** rather than duplicated. Discord will happily hold
two `#shop` channels and they are indistinguishable in a picker afterwards.

It is its own command rather than a button in `/admin` because `/admin` is gated on
`is_staff`, and on a fresh server `STAFF_ROLE_IDS` is empty — so `is_staff` is False for
everyone including the owner, and a setup button inside it could never be pressed. The
bot needs **Manage Channels** and **Manage Roles**; `/setup` names them up front rather
than discovering the gap halfway through and leaving the server half-built.

Required before the first boot:

| Name | Missing means |
|---|---|
| `DISCORD_TOKEN` | the bot cannot log in |
| `NOLA_GAME_SEED_SECRET` | `core/games.py` refuses to boot — ≥20 chars, and not the placeholder literal that ships in the source |
| `GUILD_ID` | slash commands sync nowhere |
| `OWNER_DISCORD_IDS` | nobody can fund a treasury, so every payout fails |

Website only — the public pages work without these, but the sign-in button does not
appear: `NOLA_DISCORD_CLIENT_ID`, `NOLA_DISCORD_CLIENT_SECRET`,
`NOLA_DISCORD_REDIRECT_URI`, `NOLA_STAFF_DISCORD_IDS`.

Reference market (CONTRACT.md section 12) — both optional, both have working defaults:
`NOLA_REFMARKET_URL` (the other market's origin) and `NOLA_REFMARKET_ENABLED` (set to
`0` to switch the six-hourly pull off from the panel without a deploy). Neither is a
secret; the endpoint is public.

## Domain and TLS — neworleansshop.org

**Not a Cloudflare tunnel**, though `.env.example` said so at first. A tunnel needs
`cloudflared` running beside the web process; this container has no shell and one
process slot, so there is nothing to run it. Cloudflare's proxy points at the panel's
port allocation directly instead:

| Step | Where | Value |
|---|---|---|
| 1 | registrar (where the domain was bought) | change nameservers to the two Cloudflare gives you |
| 2 | Cloudflare → DNS | `A` record, name `@`, content = the **IP** from Wispbyte → Network, **Proxied** (orange cloud) |
| 3 | Cloudflare → DNS | `CNAME` `www` → `neworleansshop.org`, Proxied |
| 4 | Cloudflare → Rules → Origin Rules | *Rewrite to* → **Destination Port** → the **port** from Wispbyte → Network |
| 5 | Cloudflare → SSL/TLS → Overview | **Flexible** |
| 6 | Discord Developer Portal → OAuth2 → Redirects | `https://neworleansshop.org/auth/callback` |

Step 5 is Flexible because the origin is plain HTTP on a game-panel allocation with no
certificate of its own. Cloudflare terminates TLS at the edge, which is what makes the
`https://` callback Discord insists on possible at all. Full/Strict would require a
certificate on the origin, which the panel cannot issue.

Step 4 is the one people skip. Cloudflare's proxy only accepts visitor traffic on the
standard ports; the allocation is not one of them, so without the Origin Rule the edge
tries port 443 on the origin and gets nothing.

### The port the site binds

`run_web.py` resolves, in order: `SERVER_PORT` (injected by Pterodactyl — the
allocation), `PORT` (most PaaS hosts), `NOLA_WEB_PORT` (ours), then `8080`. Host first,
deliberately: the panel routes to the allocation it chose, and a number we picked is
only meaningful when nothing else decided.

Binding the wrong port **succeeds** — the process starts clean, prints nothing unusual,
and every request from the panel arrives at a port with nothing on it. With no shell
there is nothing to inspect afterwards, so the bound address and the variable it came
from are printed at startup:

    web: binding 0.0.0.0:25580 (from SERVER_PORT)

`0.0.0.0`, never `SERVER_IP`: inside a container that variable can hold the
allocation's public address, which is on no local interface, and binding it fails
outright.

## Running the tests

    python run_tests.py

**Not `pytest`.** Every file in `tests/` is a script that runs its checks at
import and exits non-zero on failure, and that shape is load-bearing:
`test_no_wagering_on_web.py` asserts that nothing reachable from `web/` has
pulled `core.games` or `core.predictions` into `sys.modules`, which is only
meaningful in an interpreter where no betting test ran first; and each
DB-backed file points `NOLA_DB_PATH` at its own temporary database before
importing `core.db`, which resolves that path once, at import.

`pytest` imports them all into one process and hits both at once — an
INTERNALERROR from the first, foreign-key errors from the second. Neither is
a bug in the tests. `run_tests.py` gives each file its own interpreter.

## Boot diagnostics

With no shell, the boot block is the only diagnostic there is. It prints the
database path, journal mode, foreign-key state, catalog size and treasury balances,
and warns outright when every treasury is empty — an unfunded treasury is otherwise
invisible until someone tries to get paid.
