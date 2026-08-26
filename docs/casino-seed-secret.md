# NOLA_GAME_SEED_SECRET

`core/games.py` derives every round's `server_seed` from one persistent,
private value: `NOLA_GAME_SEED_SECRET`. This document is what
`.env.example` points to.

## Why this must be a real secret

The outcome of every coinflip/dice round is
`HMAC(server_seed, f"{client_seed}:{nonce}")`, and
`server_seed = HMAC(NOLA_GAME_SEED_SECRET, round_id)`. The whole
provably-fair scheme (CONTRACT.md section 9) rests on the player being
unable to predict `server_seed` before it is revealed at settlement. That
is only true if `NOLA_GAME_SEED_SECRET` is private and hard to guess.

This module used to fall back to a literal, public default
(`"dev-insecure-seed-secret-change-me"`) when the env var was unset. That
default is committed to source control, and the derivation algorithm is
documented in this same file's own module docstring -- so an unset
`NOLA_GAME_SEED_SECRET` did not mean "less random," it meant **anyone who
has read the code can predict every round's outcome before placing a bet**.
`tests/test_betting_attack.py` demonstrates this: an attacker who knows
only the public default and the public algorithm wins 20/20 coinflips.

`core/games.py` no longer has an insecure default. It refuses to derive a
seed -- and the bot refuses to boot (see below) -- unless a real secret is
configured.

## What "real" means here

Enforced by `core/games._check_seed_secret()`:

- **Set.** An empty or missing `NOLA_GAME_SEED_SECRET` is refused.
- **Not the old placeholder.** The literal
  `"dev-insecure-seed-secret-change-me"` is rejected by value, explicitly,
  even though it is long enough to otherwise pass the length check --
  it is a known, public string and must never be usable in production.
- **At least 20 characters** (`games.MIN_SEED_SECRET_LENGTH`), so a short,
  guessable literal cannot pass just by being "not the default."

## How to generate one

```
python -c "import secrets; print(secrets.token_hex(32))"
```

That prints 64 hex characters (32 bytes of real randomness). Put the
result in `NOLA_GAME_SEED_SECRET` in your `.env` (or the Wispbyte panel's
environment variables) -- never in this repo, never in a commit, never in
`.env.example`.

## What happens if it's missing or bad

Two independent checks, both loud, both printing the exact env var name
and the exact command above:

1. **At boot** (`core/games.configure()`, called from `bot/main.py`'s
   `build_bot()` right alongside the existing config check): the process
   prints one `FATAL: ...` line and exits before it loads a single cog or
   opens a single round. This is the load-bearing check -- an operator on
   a host with no shell sees one unambiguous line in the process log
   telling them what to set and how to generate it, instead of the casino
   quietly running in an unsafe state until a player notices they cannot
   lose.
2. **At every seed derivation** (`core/games._server_seed_for()`), as a
   defense-in-depth backstop for any caller that reaches `core.games`
   without going through the bot's boot sequence.

Neither check happens lazily "at the first bet" as the only signal --
by the time a bet is placed the deployment is already live, which is too
late to be catching this for the first time.

## Rotating it

Rotating `NOLA_GAME_SEED_SECRET` is safe for rounds that have already
settled -- their real seed was already revealed and stored on the row;
nothing about it depends on the process secret any more.

It is **not** safe to rotate while a round is open (committed but not yet
settled): `settle_round` can only reveal that round's seed by re-deriving
it from the *same* secret that was in effect when the round opened. If the
secret has changed in between -- most likely an unpersisted process
restart with a newly-set env var -- the freshly-derived seed will not match
the round's committed `server_seed_hash`. There is no way to recover the
original seed (it was deliberately never stored while the round was open),
so there is no fair outcome left to compute for that round.

In that case `settle_round` does **not** raise and leave the bet's hold
stranded. It voids the round (`game_rounds.state = 'voided'`) and refunds
every bet still open on it in full (`money.release_hold`, never a
capture) -- the same shape as `predictions.void()`. Nobody's money is
ever frozen or lost because the operator restarted a process.
