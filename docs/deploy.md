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

## Boot diagnostics

With no shell, the boot block is the only diagnostic there is. It prints the
database path, journal mode, foreign-key state, catalog size and treasury balances,
and warns outright when every treasury is empty — an unfunded treasury is otherwise
invisible until someone tries to get paid.
