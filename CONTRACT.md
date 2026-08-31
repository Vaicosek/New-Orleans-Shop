# New Orleans — build contract

Market **New Orleans**, Discord server **Diplomatica**. Own bot, own website, own economy.
Code patterns lifted from AbexTech; nothing shared at runtime.

Written 2026-08-26. This file is the contract. Nothing gets built that contradicts it;
if something must contradict it, this file changes first.

## 1. Locked decisions

| Thing | Decision | Why |
|---|---|---|
| Codebase | Standalone project in `New Orleans Shop`, patterns lifted from AbexTech | John: "it does not [relate], i am just using its code" |
| Economy | Fully separate DB, wallet, currency | No path from a NOLA bug to live Abex money |
| Wagering | Discord only. **Never on the website.** | John: "its only for discord inside that discord its not on website i own" |
| Money type | `INTEGER` everywhere. No floats. | AbexTech has float/int money coexisting across modules — a live inconsistency |
| Command surface | Panels. One slash command per domain. | 136 -> 15 was the whole fight last time |
| Typed IDs | Never. Pickers, autocomplete, `/go <code>` addresses. | A modal asking for an ID is a design failure |
| Hosting | Wispbyte panel, no shell | Everything operable by pinging the bot or clicking UI |

## 2. Internal ids

| Kind | Value |
|---|---|
| Market id | `nola` |
| Display name | New Orleans |
| Currency | Gold ingots, symbol `g`, whole numbers only -- `core.pricing.CURRENCY` (see S5). Stale: `core.config.currency_name`/`CURRENCY_NAME` still exists and defaults to "coin" but is no longer what any price renders; dead config for the integrator to retire. |
| Wallet subjects | `u:<discord_id>`, `treasury:shop`, `treasury:games`, `treasury:house` |

Real names everywhere a user looks. `nola` never appears in user-facing text.

## 3. Repo layout

Deliberately split from day one. AbexTech's `Restocker_main.py` is 1.0 MB and everything
imports it through `sys.modules["__main__"]`; that is the one thing we are not copying.

    run_all.py            supervisor: bot process + web process, backoff, give-up
    run_shop.py           bot entrypoint
    run_web.py            web entrypoint
    core/
      db.py               connection, pragmas, schema apply, migrations
      schema.sql          all DDL, one file
      money.py            wallets, ledger, holds, claim-first, idempotency
      audit.py            append-only action log
      catalog.py          items, prices, stock
      orders.py           order lifecycle
      alerts.py           restock thresholds + alert suppression state
      games.py            casino engines, provably-fair RNG
      predictions.py      pari-mutuel markets
      config.py           typed env helpers, no bare os.getenv anywhere
    bot/
      main.py             intents, cog load, persistent views, boot self-check
      cogs/               shop, orders, wallet, casino, predictions, admin
      views/              one panel per domain + shared pickers.py
      ui/embed.py         rows(), money formatting, price-basis labelling
    web/
      server.py           aiohttp app, per-domain route registration
      shell.py            page() chrome
      theme.py            CSS tokens
      auth.py             Discord OAuth2, sessions, staff allowlist
      pages/              storefront.py, account.py, ledger.py
    docs/                 *.md — the bot reads these at runtime
    tests/

**`docs/` is a production concern.** The bot serves its own docs to users. A stale doc
means the bot recommends a dead command to a real customer. Retiring anything sweeps its
name from every user-facing string and every doc in the same commit.

## 4. Data model

SQLite, WAL, `foreign_keys=ON`, `busy_timeout=5000`, thread-local connections.
`db()` opens a transaction; `db_in(conn)` joins the caller's — never nest a fresh `db()`
inside an open transaction, it commits the caller's half-written work.

### Money

| Table | Purpose |
|---|---|
| `wallets` | one row per subject. `coins INTEGER`, `frozen`, `flags` |
| `ledger_entries` | append-only, one row per delta. Never updated, never deleted |
| `ledger_holds` | escrow. `open -> captured \| released \| expired`. `captured+released <= amount` enforced by CHECK |
| `idempotency` | PK `key`, `service`, `endpoint`, `payload_hash`, `state`, `response_json` |
| `audit_actions` | what happened, who did it, and the reverse ops to undo it |

### Shop

| Table | Purpose |
|---|---|
| `items` | catalog. `price_coins INTEGER`, `price_unit_pieces INTEGER`, `stack_size INTEGER`, `stackable` (see S5) |
| `stock` | live quantity per item, `capacity`, `updated_at` |
| `stock_alerts` | per-item threshold + **`acked_until_qty`** (see §6) |
| `orders` | `requested_pieces`, `produced_pieces`, `status`, `market_id` |
| `order_claims` | who claimed how much. Unique constraint carries the claim-first guard |
| `addresses` | 4-char picker codes, alphabet excludes `l o 0 1` |

### Betting (Discord only)

| Table | Purpose |
|---|---|
| `pred_markets` | question, outcomes, opens_at, closes_at, resolved_outcome, rake_bps |
| `pred_stakes` | one row per stake, `hold_id` FK, `outcome`, `amount` |
| `game_rounds` | game kind, server_seed_hash, server_seed (post-reveal), client_seed, nonce |
| `game_bets` | `round_id`, `user`, `amount`, `hold_id`, `payout`, `settled_event_id` |

No betting table is ever read by any `web/` module. Enforced by test (§9).

## 5. The 64x rule — solved at the schema, not by discipline

This is the single most repeated bug in the Abex economy and it is caused by storing a
*derived* number. AbexTech stores `coin` as a **float per piece**, converting with
`price / 64.0` at input. `300 / 64 = 4.6875` — a number that cannot be an integer coin,
so the system carries floats forever and rounds differently in different call sites.

New Orleans stores the number the owner actually types, plus how many pieces that
number buys — two different things that used to be one column, and conflating them is
a silent bug of its own (see the sapling example below):

    price_coins        INTEGER NOT NULL   -- what the owner typed, verbatim
    price_unit_pieces  INTEGER NOT NULL DEFAULT 64   -- how many pieces `price_coins` buys
    stack_size         INTEGER NOT NULL DEFAULT 64   -- MINECRAFT stack size, capacity maths only

`price_unit_pieces` is the ONLY divisor a per-piece figure is ever taken against.
`stack_size` never divides a price — it feeds `barrel_slots * stack_size` capacity maths
and it names the unit in a price label ("stack of 64") when the quoted unit happens to
be a full stack. The two used to be one column; that is exactly how an item that stacks
to 64 but sells per 32 — saplings — got silently charged double.

Charge for N pieces, integer arithmetic only, one rounding rule in one function
(`core/pricing.py:charge`):

    def charge(pieces, price_coins, unit_pieces=STACK):
        # half-up, no floats, deterministic
        numerator = pieces * price_coins * 2 + unit_pieces
        return numerator // (unit_pieces * 2)

Consequences:
- No float ever touches money. `core/money.py` refuses a non-`int` amount at the boundary.
- The owner types the price and the DB stores it verbatim. Nothing is lossy at write time.
- Rounding happens once, in `charge()`, and is unit-tested against a table of known cases.
- `price_unit_pieces` must never exceed `stack_size` (enforced by a schema CHECK) — the
  quoted unit can be smaller than a stack, never bigger than one.

**Worked example — the case the split exists for.** Saplings sell 1 gold per 32 pieces
but stack to 64 in Minecraft: `price_coins=1`, `price_unit_pieces=32`, `stack_size=64`.
A full 64-piece stack order is `charge(64, 1, 32)` = `(64*1*2 + 32) // 64` = `2` gold —
**not** 1. Treating `stack_size` as the price divisor (the bug this split exists to make
impossible) gives `charge(64, 1, 64)` = 1 gold instead, silently halving the price on
every full-stack sapling order. Contract and code must always agree on which of the two
numbers `charge()` divides by; `price_unit_pieces` is the only correct answer.

**Every user-facing price row states both bases**, via `core.pricing.price_label()`.
Never a bare number:

    Honeycomb Block    300 g / stack of 64   ·   4.69 g / piece
    Honey Block         350 g / stack of 64   ·   5.47 g / piece
    Sapling                1 g / 32           ·   0.03 g / piece

Defaults from the brain, already correct, do not "fix" them:
Honeycomb Block 300 g/stack, Honey Block 350 g/stack.

**Currency.** Gold ingots. Whole numbers — there is no fractional gold, so nothing in
`core/pricing.py` ever needs a decimal type. Symbol `g`, printed after the number
(`300 g`, never `g300` or a bare `300`). `core.pricing.CURRENCY` is the one place the
symbol is defined; nothing else hardcodes it.

## 6. Restock alerts — with real suppression

AbexTech's alarm recomputes from scratch on every scan and its "Acknowledge" button only
disables the components on that one Discord message. It writes nothing. The same DM
repeats on every scan until the stock moves.

New Orleans: `stock_alerts.acked_until_qty`. Acknowledge writes the current quantity.
The alert fires only when `qty < threshold AND qty < acked_until_qty` — so it goes quiet
after an ack and speaks again only if the situation gets *worse*, or resets when restocked
above threshold. No timers, no cron state to drift.

## 7. Discord surface

One slash command per domain. Everything else is a panel.

| Command | Opens | Contains |
|---|---|---|
| `/shop` | Shop panel | stock table, item picker, price lookup, "order this" |
| `/orders` | Orders panel | open board, claim, mark fulfilled, manager approve |
| `/wallet` | Wallet panel | balance, held, history, transfer (UserSelect -> modal) |
| `/casino` | Casino panel | game picker, bet, round history, fairness verify |
| `/predict` | Predictions panel | open markets, stake, my positions |
| `/admin` | Admin panel | items, prices, thresholds, resolve markets, treasury |
| `/go <code>` | direct jump | 4-char address to any entity |

Rules the panels must obey:

- **Picker then modal, never a text field for an identity.** A Modal cannot autocomplete
  and cannot hold a Select; a View cannot hold free text. So: `View(UserSelect|Select)` ->
  callback opens `Modal` carrying the already-resolved object. Free text only for genuinely
  free text (a note, a quantity).
  Do not copy `views/market_settings.py:492` `_PeopleModal` — it asks for a typed Discord
  user id and predates the rule.
- **Defer inside 3 seconds**, then `followup.send`. Any handler touching DB, Discord posting
  or DMs will blow the interaction window otherwise.
- **Persistent views re-resolve their subject from the message**, never from `self`.
  Registered at boot with placeholder state, so `self` is a lie after a restart.
- **Empty states are empty.** No placeholder rows, no "no data yet" decoration beyond one line.
- **No emoji in embed titles.** Status glyphs in rows are fine.
- **One line if it can be one line.**

## 8. Money rules — non-negotiable

Carried from lessons already paid for. Each of these has a bug behind it.

1. Every money move is **one `UPDATE ... WHERE <entire precondition>`**, checked by
   `rowcount`. Never read-then-write. Never `MAX(0, x - ?)` — clamping to zero instead of
   failing is how a system silently pays money it does not have.
2. **Claim first, then act.** Mark the row charged in one atomic statement; act only if you
   won; release the claim on failure. Act-then-mark double-pays on any crash between.
3. **Validate fully before claiming**, so a losing concurrent retry never gets a
   success-shaped replay for a request that would have failed anyway.
4. **Idempotency keys are minted at the source event**, never reconstructed from a
   timestamp — reconstructed keys drift between runs and duplicate.
5. **Payload hash stored with the claim.** Same key, different payload is a loud conflict,
   never a silent overwrite.
6. **The audit row and the balance write commit in the same transaction.** Not a best-effort
   side call.
7. **Only `treasury:*` may go negative**, with a hard floor, and it screams when it does.
8. **`treasury:games` cannot mint.** It gets transfer and hold scopes only. A betting bug can
   misallocate money; it can never create money.
9. **`gambling_blocked` is a wallet flag the games service cannot set, clear or read as
   trusted.** Enforced at **hold** time, not capture — the hold is the first money-committing
   step of a bet.
10. **Preview with real figures, then confirm.** Typed confirmation for the irreversible —
    and the typed string is a **name**, never an id.
11. **A missing or zero price is a loud failure at payout**, never a silent zero-coin payment.
12. **Every ledger entry carries a reason** tying it to an order, round or market id.

## 9. Betting model

**Prediction markets — pari-mutuel.** Players stake against each other. Pool splits pro-rata
to correct outcome. `rake_bps` configurable, **default 0**. Stake places a *hold*; resolution
captures losers and credits winners in one transaction keyed on the market's resolve event id.
Resolution is staff-only, preview-then-confirm, typed market name.

**Casino — house-banked.** Small on purpose: coinflip and dice first. House edge is an explicit
config number per game, never an emergent property of the payout maths.

**Provably fair, both.** Commit `sha256(server_seed)` before the round, reveal `server_seed` at
settlement, mix with a player-supplied `client_seed` and a `nonce`. `/casino` panel has a
"verify" view that recomputes any past round. Cheap to build, and it is the difference between
a game and a black box.

**Guardrails** (config, all enforced server-side at hold time):
`MAX_BET`, `MAX_DAILY_LOSS`, `MIN_ACCOUNT_AGE_DAYS`, plus the `gambling_blocked` self-exclusion
flag. Borrowed or held coins can never fund a wager.

**The web boundary is tested, not trusted.** `tests/test_no_wagering_on_web.py` imports every
module under `web/` and fails if any of them references a betting table, route, nav entry or
string — mirroring AbexTech's own `test_no_wagering_surface.py`, scoped to the website only.

## 10. Website

**Domain: `neworleansshop.org`** (registered 31 Aug 2026). The `.com` is held by a
parking service and was never an operating business; the name itself is not a
registrable mark -- "New Orleans" plus a generic noun is primarily geographically
descriptive under Lanham Act S2(e)(2) -- so there is nothing to collide with. The
market's Discord display name stays **New Orleans**; `nola` remains internal only (S2).

Reached over a Cloudflare tunnel pointed at `NOLA_WEB_PORT`. The OAuth2 redirect URI
is `https://neworleansshop.org/auth/callback` -- the public https address, never
localhost and never the tunnel's internal port, and it must match the Developer
Portal entry byte for byte.

Three audiences, one shell.

| Route | Who | Notes |
|---|---|---|
| `/` | public | storefront: what New Orleans stocks, what it costs |
| `/stock` | public | live table, both price bases |
| `/me` | customer | Discord OAuth2. Own orders, balance, history |
| `/ledger` | staff | internal: balances, orders, payouts, audit trail |
| `/health` | public | must answer when the bot is down |

Public routes take no session and touch no bot state — they answer when the bot is down.
Auth is **OAuth2 only** (no `/website_login` code-mint path; that only makes sense bundled
with an existing bot). One cookie, one session store, and **one identity function** that every
page resolves through. Staff is a Discord-ID allowlist, checked at the route, and the staff nav
entry is omitted server-side rather than CSS-hidden.

### Design

Cousin: **a commodity exchange price sheet** — the printed daily sheet a New Orleans broker
would have pinned up. Plain rules, tabular figures, one ink colour plus black. Rendered dark.
That is the reference; it is not "a dashboard".

Tokens lifted from `abex_theme.py`, which John has already accepted:

    --ground #1b1d20   --raised rgba(239,236,229,.05)   --line #3b3e43
    --text   #efece5   --dim #aaa59b                    --inert #6f6c66
    --gain   #8fbf6a   --loss #d87a6a                   --accent #c9b37a
    font: Georgia, 'Times New Roman', serif  (figures too, tabular-nums)

One change: **base font 17px, not 15px.** The live Abex theme is 15px, but on the ledger-v1
mockup the accepted note was "bigger and more readable — 15 -> 18, tables looser, lighter text".
17 is that, one notch back for a table-dense page. Flagging the discrepancy rather than
silently picking one.

Banned outright: tinted cards, 1px grey borders as decoration, left-border strips, glass,
glow, gradients, Inter/Geist/Space Grotesk, monospace-for-vibe, CAPS eyebrow labels, chips,
stat-card hero rows, three-card rows, bento grids, `~` approximated dates, subtitle sentences
under headings, scroll fade-ins.

## 11. Deploy

Wispbyte panel. One startup command, no shell.

    APP PY FILE    run_all.py
    REQUIREMENTS   requirements.txt

`run_all.py` supervises the bot and the web process as children: prefixed unbuffered output,
exponential backoff, gives up on a child after N rapid failures instead of restart-storming,
forwards SIGTERM so a panel Stop closes the gateway cleanly. Boot runs a read-only self-check
that resolves every configured guild/channel id and prints one readable block — the only
diagnostic available without a shell.

`.env` keys are documented in `docs/deploy.md` by NAME only. No token, webhook, secret or
access-granting id is ever written into this repo or the brain.

## 12. Open — John decides

1. ~~**Currency name.**~~ Decided: gold ingots, symbol `g`, whole numbers (S2, S5). `core.config.currency_name`/`CURRENCY_NAME` is leftover from before that decision and no longer drives anything a price renders -- dead config for the integrator to retire.
2. **Casino games beyond coinflip and dice.** Blackjack and roulette are real work; worth it
   only if people will actually play them.
3. **Prediction market rake.** Default 0 — you take nothing. A rake makes you the house in a
   way pari-mutuel otherwise does not.
4. **Who is staff on Diplomatica.** Discord IDs for the allowlist, when you have them.
