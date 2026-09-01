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
| Command surface | Panels. One slash command per domain, plus `/setup` at install (S7). | 136 -> 15 was the whole fight last time |
| Typed IDs | Never. Pickers, autocomplete, `/go <code>` addresses. | A modal asking for an ID is a design failure |
| Hosting | Wispbyte panel, no shell | Everything operable by pinging the bot or clicking UI |

## 2. Internal ids

| Kind | Value |
|---|---|
| Market id | `nola` |
| Display name | New Orleans |
| Currency | Gold ingots, symbol `g`, whole numbers only -- `core.pricing.CURRENCY` (see S5), the one place the symbol is defined. The old `core.config.currency_name`/`CURRENCY_NAME` setting, which defaulted to "coin", has been **retired** -- it no longer exists in `core/config.py` and nothing reads it. |
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
    docs/                 *.md — written for the humans who deploy and maintain this
    tests/

**`docs/` is for the humans maintaining the system, not for users.** Nothing under
`bot/` reads `docs/` at runtime -- verified by grep across `bot/`, `web/`, `core/` and the
`run_*.py` entrypoints, which returns no reader at all. `deploy.md`, `casino-seed-secret.md`
and `shop-buildout.md` are operator documents: the person with the Wispbyte panel is their
only audience. An earlier draft of this contract asserted the bot served its own docs to
users and that a stale doc would make it recommend a dead command to a real customer; that
was never built and is not planned here. If a `/help` that reads `docs/` is ever wanted, it
is new work and this section changes first.

The sweep rule stands regardless, and is the reason this directory is still named in the
contract: **retiring anything sweeps its name from every user-facing string AND every doc in
the same commit.** A stale doc misleads the operator instead of the customer, which is how a
misdeployment happens rather than a bad recommendation -- a smaller blast radius, not none.

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
| `/setup` | Setup preview | builds the server's channels and roles, once, at install |
| `/go <code>` | direct jump | 4-char address to any entity |

`/setup` is the one command that is not a domain panel, and it is separate rather than a
button inside `/admin` for a functional reason, not a cosmetic one: `/admin` is gated on
`is_staff`, which reads `STAFF_ROLE_IDS` -- and on a fresh server that list is empty, so
`is_staff` is False for **everyone**, the server's owner included. `/admin` is therefore
unusable until setup has run, and a setup button living inside it could never be pressed.
It is gated on `OWNER_DISCORD_IDS` **or** the guild's actual owner, for the same
chicken-and-egg reason: that list may not be filled in yet either.

It is idempotent and previews before it acts. Running it twice creates nothing the second
time; a channel someone already made by hand with a matching name is **adopted**, not
duplicated, because Discord allows two `#shop` channels and the pair is indistinguishable
in a picker afterwards.

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

**Casino — house-banked.** Small on purpose: coinflip, dice and slots. House edge is an explicit
config number per game, never an emergent property of the payout maths. Slots pays a
different multiplier per symbol combination (`GAME_CONFIG["slots"]["payout_table"]`) instead
of one flat payout, but the same rule applies: RTP and edge are derived algebraically from
the reel weights and pinned by a regression test, never left as a byproduct of the payout maths.

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

## 10. Auctions model

**Public open-bid (English), not a wager.** A single winner pays the top bid; a bidder can
never lose more than the price of the lot they actually win, which is exactly why auctions do
**not** run through `core/wagering.py`'s `MAX_BET`/`MAX_DAILY_LOSS` guard (S9's guardrails are
about capping real risk of loss, not every hold in the system) and do not touch
`gambling_blocked`. Bidding is commerce, same family as an order: it is gated by the
`orders_blocked` wallet flag instead, checked explicitly in `core/auctions.py::bid` since
`money.place_hold`'s automatic gate only covers `money.GAMBLING_SERVICES`.

**The first house-sells-to-player money flow.** Every other flow in this codebase pays the
other direction: `orders.py` is a worker-cooperative production economy where `treasury:shop`
pays workers, never the reverse. An auction's winning bid is captured *into* `treasury:shop`.
Auctions never touch `stock.pieces` or `items.active` -- handing over the physical item is a
manual staff task, exactly like an order's delivery; the `pieces` column on a listing is purely
descriptive.

**Lifecycle:** staff opens a lot against a real catalog item (never a typed name) with a
minimum bid, minimum raise, and duration. Each bid places a fresh hold for its full amount;
the moment a higher bid supersedes it, the previous leader's hold is released -- the new hold
is always placed *before* the old one is released, so a challenger who cannot afford their bid
never costs the current leader their escrow. When `closes_at` passes, the lot closes and
settles automatically (a one-minute sweep loop in `bot/cogs/admin.py`): the winning hold is
captured to `treasury:shop`, or the lot settles with no winner if nobody bid. Unlike a
prediction market, an auction's outcome is the objective top bid at close, not a subjective
staff call, so there is no insider-window risk in settling it the instant it closes and no
staff "resolve" step exists. `void` (staff-only, pre-settlement) refunds the current leader in
full and cancels the listing -- the escape hatch for a lot listed by mistake.

**No new slash command.** Creation and voiding live in `/admin`, next to every other
staff-only, money-deciding action. Bidding lives entirely on the auction's own persistent
public card in the auctions channel -- a Bid button, an amount modal, and a confirm gate --
mirroring `bot/views/orders.py`'s order card pattern down to resolving the auction id from the
message's own embed footer rather than trusting `self`.

## 11. Loyalty ranks

**Adapted from AbexTech's `abex_tiers.py`, not copied.** AbexTech blends two
halves into one score -- points earned, plus a wallet balance counted at a
capped rate -- against a five-rung ladder (Recruit, Worker, Veteran, Expert,
Elite), and pays out a purchase discount on top. New Orleans keeps the
blended-score shape and the ladder names/thresholds (already-tuned numbers
from a live economy of the same kind), but there is no customer-pays-for-
items flow here to discount: orders pay *workers* out of `treasury:shop`,
they are not a customer buying something. So what counts, and what a rank
changes, had to be re-derived for this shop specifically:

**Points, computed live, never cached** (`core/loyalty.py`): coins actually
paid to a worker for a fulfilled order-claim, plus coins actually spent
winning an auction lot, both divided by `POINTS_DIVISOR`. A wallet's held
balance counts too, at a capped rate that can at most DOUBLE what was
earned -- park a fortune and produce nothing, and rank stays Recruit,
because half of nothing is nothing. Same discipline as `core/wagering.py`'s
exposure query: re-derived from `order_claims`/`auction_bids` on every
read, never a running counter that can drift from what actually happened.

**What a rank actually changes**, each wired at the exact point the money
moves, never trusted from a stale read:
  - `payout_bonus_pct` -- added on top of the priced amount at
    `orders.approve()` time (ported from AbexTech's own "work" domain
    benefit). The bonus itself is real money paid from `treasury:shop`, and
    `order_claims.paid_coins` records the TOTAL actually paid -- a bonus a
    worker was really paid counts toward their OWN future points same as
    the base amount does.
  - `bet_bonus_pct` -- raises the effective MAX_BET/MAX_DAILY_LOSS a
    subject gets in `core/wagering.py`, read in the SAME transaction as the
    exposure check. This stands in for AbexTech's purchase discount, which
    has nothing to attach to in this economy. A Recruit (0% bonus, true of
    every fresh wallet) sees the bare base constants, unchanged.

**A staff override wins outright over the computed score** (`/admin` ->
Set rank / Clear rank override, owner-only -- the same class of privilege
as Fund treasury, since a forced rank grants real payout and betting-cap
benefits). One row in `loyalty_overrides`, same shape as a wallet flag:
last write wins, cleared by deleting it.

**Discord role auto-sync, never a staff click.** `/setup` builds one role
per rank (`role:rank:<key>`, section 7's channel/role provisioning). The
bot moves a subject's role to match their current tier right after the
moment their score could have changed: an order payout, an auction
settlement, or a staff override -- `bot/loyalty_sync.py`, best-effort and
never allowed to fail the money move beside it.

**The website shows the rank and the order-side bonus only -- never the
betting-cap one.** Section 9's wall (`tests/test_no_wagering_on_web.py`'s
word scan: "bet", "payout" and the rest, banned as whole words anywhere
under `web/`) is not just a vocabulary rule; the site must never let on
that a casino exists at all. `/me` shows a player's own rank and order
bonus; `/ledger` shows every wallet's rank to staff. Neither ever mentions
the betting-cap benefit -- that one is Discord-only, shown in `/wallet`.

## 12. Website

**Domain: `neworleansshop.org`** (registered 31 Aug 2026). The `.com` is held by a
parking service and was never an operating business; the name itself is not a
registrable mark -- "New Orleans" plus a generic noun is primarily geographically
descriptive under Lanham Act S2(e)(2) -- so there is nothing to collide with. The
market's Discord display name stays **New Orleans**; `nola` remains internal only (S2).

**Not a Cloudflare tunnel.** `cloudflared` has to run alongside the web process, and
the Wispbyte container has no shell and one process slot. The site is instead reached
through Cloudflare's proxy pointed straight at the panel's port allocation: a proxied
`A` record, an Origin Rule rewriting the destination port to that allocation, and
SSL/TLS in Flexible mode so the edge terminates https over a plain-http origin.

The process binds `SERVER_PORT` -- the allocation Pterodactyl injects -- ahead of any
port of our own (S11). The OAuth2 redirect URI is
`https://neworleansshop.org/auth/callback`: the public https address, never localhost
and never the allocation's port, matching the Developer Portal entry byte for byte.

Three audiences, one shell.

| Route | Who | Notes |
|---|---|---|
| `/` | public | storefront: what New Orleans stocks, what it costs |
| `/inventory` | public | live table, both price bases |
| `/stock` | public | the same page under the name it had before the owner renamed it. Same handler, not a redirect — a URL somebody has already pasted into Discord is a promise, and a rename is not a reason to break one |
| `/me` | customer | Discord OAuth2. Own orders, balance, history |
| `/order` | customer | POST only. Opens a production/restock request -- the site's one write route |
| `/ledger` | staff | internal: balances, orders, payouts, audit trail |
| `/health` | public | must answer when the bot is down |

Public routes take no session and touch no bot state — they answer when the bot is down.
Auth is **OAuth2 only** (no `/website_login` code-mint path; that only makes sense bundled
with an existing bot). One cookie, one session store, and **one identity function** that every
page resolves through. Staff is a Discord-ID allowlist, checked at the route, and the staff nav
entry is omitted server-side rather than CSS-hidden.

**The storefront is a grid, not a table** — each item shows a Minecraft item/block icon
(bundled at `web/assets/icons/*.png`, inlined as a `data:` URI by `web/icons.py` so the page
still needs no network to render; an item with no mapped icon gets a plain monogram tile, never
a broken image request), its name, its price, and how many are on hand. Still **no cards** —
the rule below stands. Items are separated by whitespace and the same hairline rule the
price-sheet tables already use, never a filled or bordered box.

**`/order` is the site's one exception to "no session" above and its first state-changing
route.** Signed-in only; anonymous visitors get a "sign in to order" link, never controls that
look live but cannot be submitted. A signed-in visitor checks off any number of items across
the grid -- a plain checkbox per item, no cart page, no JavaScript anywhere on this site -- and
picks a quantity for each with a radio-pill group (1 stack / 4 stacks / 16 stacks, or a typed
"Custom" amount), then one submit opens a production/restock request for every checked item in
a single POST. Each one goes through `core.orders.create_order` independently -- the same
function, same validation, same audit trail Discord's shop panel uses -- so one bad line (a
stale item id, a since-emptied custom field) never blocks the rest of the batch; only a fully
empty result is a hard 400. The redirect reports exactly how many orders opened and, when some
did not, how many were skipped. No money moves here; that only ever happens at `/orders`
approval in Discord. CSRF-protected the same way `/logout` is (the session's own token, checked
with `secrets.compare_digest`). The web process holds no live Discord connection (see section
13's process split), so an order opened from the site is not pushed to the orders channel -- it
surfaces to workers the same way a card that failed to post already does: it exists and is
claimable from Discord's `/orders` immediately, no push required.

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

## 13. Deploy

Wispbyte panel. One startup command, no shell.

    APP PY FILE    run_all.py
    REQUIREMENTS   requirements.txt

`run_all.py` supervises the bot and the web process as children: prefixed unbuffered output,
exponential backoff, gives up on a child after N rapid failures instead of restart-storming,
forwards SIGTERM so a panel Stop closes the gateway cleanly. Boot runs a read-only self-check
that resolves every configured guild/channel id and prints one readable block — the only
diagnostic available without a shell.

The three channel ids are **not** required env vars. A fresh server has no channels to
name, and a panel-only host has no shell to add them afterwards, so `/setup` provisions
the layout and `guild_layout` holds the ids; the env vars remain as an override and win
over the table when set. `DISCORD_TOKEN`, `GUILD_ID`, `NOLA_GAME_SEED_SECRET` and
`OWNER_DISCORD_IDS` are still required before first boot.

`.env` keys are documented in `docs/deploy.md` by NAME only. No token, webhook, secret or
access-granting id is ever written into this repo or the brain.

## 14. Reference market — read-only, and it stays that way

New Orleans mirrors one other server's public market so the owner can see what an item
fetches somewhere with real volume before he sets a price here. Source: DiplomaticaMC's
market at `market.diplomaticamc.com`, endpoint `GET /api/market?limit=&offset=`, page cap
500, catalogue ~765 items — **two requests per cycle, four cycles a day.**

**It never writes a price.** Their figures are in a different currency on a different
server: `0.0157` there and `3 g` here are not two measurements of one thing, and code that
treated them as one would produce a confident wrong number. The absolute prices do not
convert and are never presented as if they do. What reads across is the shape — which of
our items people over there are short of — and reading shape is a person's job. No path in
this project may write `ref_market` into `items`, and the staff page offers no button that
would.

**It never fails anything.** `core/refmarket.pull()` returns `(rows, error)` and does not
raise. A failed cycle logs one line, records the error, and leaves the previous mirror in
place. The shop is the product; the feed is a convenience.

**It is a polite client of somebody else's server.** Their `robots.txt` grants
`User-agent: *` `Allow: /` with `Content-Signal: use=reference` — which is exactly what
this is — while naming and disallowing the AI training crawlers. So: an honest
`User-Agent` naming this shop and its site, never a spoofed browser; on `429` or `503` the
cycle stops where it stands and waits for the next one, with **no retry**; and the loop
starts after the gateway is ready rather than at process start, so a container restart loop
cannot turn into a request storm. `NOLA_REFMARKET_ENABLED=0` switches the whole thing off
from the panel without a deploy.

**Storage.** `ref_market` holds one row per (source, item_key), **replaced whole** each
cycle so a delisted item disappears instead of sitting there at a stale price. `price` is
REAL while every coin in this database is an integer — that is deliberate and is the mark
that this column is a quoted observation and not money. No history is kept: the source
publishes its own 7-day trend, and 765 rows every six hours forever would cost more than
it tells anyone.

**Health is measured on the last SUCCESS, not on the rows.** `ref_market_runs` separates
`ok_at` from `attempted_at` because a feed that died three days ago still has a full table
and would otherwise pass its own check. The boot block prints that line.

**Matching.** `core.refmarket.match_name()` is the single rule that lines their catalogue
up with ours: lowercase, strip the namespace, keep letters and digits.
`minecraft:smooth_stone`, `SMOOTH_STONE` and `Smooth Stone` all become `smoothstone`.
Deliberately exact — a fuzzy matcher would pair `Oak Log` with `Oak Wood` and hand the
owner a price for the wrong item, which is worse than no price. It is written **once**, in
Python, and the staff view joins in Python through the same function; a second copy of the
rule in SQL would be two rules that have to agree forever, failing as an empty column
rather than an error. Unmatched items still appear, with empty columns.

**Blocked as of the first live run.** The first cycle from the panel returned **HTTP 403**.
robots.txt grants `use=reference`, but the live edge is the enforcement and it wins over
the file. There is no fix on our side that is not evasion: putting a browser
`User-Agent` on this client to get past a 403 is precisely the behaviour the block exists
to stop, and this project will not do it. `403`/`401` are therefore terminal for the cycle,
logged with a line naming whether Cloudflare or their application refused, and the way
through is to **ask DiplomaticaMC's operator** to allow this client — or to set
`NOLA_REFMARKET_ENABLED=0` and leave it off. Everything downstream (schema, parser, staff
view, health line) is built and tested against a real captured payload and will work the
moment a request gets through; the boot block's `reference market:` line reading
`last success` is the only thing that counts as verification.

## 15. Open — John decides

1. ~~**Currency name.**~~ Decided and DONE: gold ingots, symbol `g`, whole numbers (S2, S5). The leftover `core.config.currency_name`/`CURRENCY_NAME` setting has been **removed** -- it is gone from `core/config.py` and no code reads it. Nothing is left for the integrator to retire here; the only surviving mention is a historical note in a `bot/ui/embed.py` docstring recording that an earlier version took the argument.
2. ~~**Casino games beyond coinflip and dice.**~~ Decided and DONE: slots shipped (commit-reveal per-reel draws via `_uniform_int_positioned`, RTP 91.975%, edge 8.025%). Blackjack and roulette remain undecided -- real work, worth it only if people will actually play them.
3. **Prediction market rake.** Default 0 — you take nothing. A rake makes you the house in a
   way pari-mutuel otherwise does not.
4. **Who is staff on Diplomatica.** Discord IDs for the allowlist, when you have them.
