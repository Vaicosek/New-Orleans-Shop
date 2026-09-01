-- New Orleans — schema.
-- Every invariant that CAN live in the database DOES live in the database.
-- A CHECK constraint is cheaper than a code review and it does not forget.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- money

-- One row per subject. Subjects are 'u:<discord_id>' or 'treasury:<name>'.
-- deficit_floor is how far negative this subject may go: 0 for every user,
-- non-zero only for treasuries. The CHECK is the last line of defence under
-- the WHERE-clause guards in core/money.py.
CREATE TABLE IF NOT EXISTS wallets (
    subject        TEXT    PRIMARY KEY,
    coins          INTEGER NOT NULL DEFAULT 0,
    deficit_floor  INTEGER NOT NULL DEFAULT 0 CHECK (deficit_floor >= 0),
    frozen         INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1)),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (coins >= -deficit_floor)
);

-- Closed vocabulary on purpose: an unknown flag name is a typo, not a feature.
CREATE TABLE IF NOT EXISTS wallet_flags (
    subject  TEXT NOT NULL REFERENCES wallets(subject) ON DELETE CASCADE,
    flag     TEXT NOT NULL CHECK (flag IN ('gambling_blocked', 'orders_blocked', 'staff')),
    set_by   TEXT NOT NULL,
    set_at   TEXT NOT NULL DEFAULT (datetime('now')),
    note     TEXT,
    PRIMARY KEY (subject, flag)
);

-- A staff-forced loyalty rank, overriding the computed score outright --
-- same shape as wallet_flags: one row per subject, last write wins. Cleared
-- by deleting the row, which reverts the subject to their computed rank.
-- rank_key values are core/loyalty.py's TIERS keys; kept in sync by hand
-- since SQLite CHECK constraints can't reference a Python table.
CREATE TABLE IF NOT EXISTS loyalty_overrides (
    subject  TEXT PRIMARY KEY REFERENCES wallets(subject) ON DELETE CASCADE,
    rank_key TEXT NOT NULL CHECK (rank_key IN ('recruit', 'worker', 'veteran', 'expert', 'elite')),
    set_by   TEXT NOT NULL,
    set_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only. Never UPDATEd, never DELETEd. `reason` is NOT NULL and
-- non-empty because an unreasoned entry is an unauditable one.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL DEFAULT (datetime('now')),
    subject       TEXT    NOT NULL REFERENCES wallets(subject),
    delta         INTEGER NOT NULL CHECK (delta <> 0),
    balance_after INTEGER NOT NULL,
    service       TEXT    NOT NULL,
    reason        TEXT    NOT NULL CHECK (length(trim(reason)) > 0),
    ref_kind      TEXT,
    ref_id        TEXT,
    idem_key      TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_subject_ts ON ledger_entries(subject, ts DESC);
CREATE INDEX IF NOT EXISTS ix_ledger_ref        ON ledger_entries(ref_kind, ref_id);

-- Escrow. A bet, an order deposit — anything committed but not yet spent.
-- amount is immutable once written; captured/released only grow, and never
-- past amount. The hold is the first money-committing step of a wager, which
-- is why gambling_blocked is enforced here rather than at capture.
CREATE TABLE IF NOT EXISTS ledger_holds (
    id          TEXT    PRIMARY KEY,
    subject     TEXT    NOT NULL REFERENCES wallets(subject),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    captured    INTEGER NOT NULL DEFAULT 0 CHECK (captured >= 0),
    released    INTEGER NOT NULL DEFAULT 0 CHECK (released >= 0),
    state       TEXT    NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open', 'captured', 'released', 'expired')),
    service     TEXT    NOT NULL,
    reason      TEXT    NOT NULL CHECK (length(trim(reason)) > 0),
    expires_at  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (captured + released <= amount)
);
CREATE INDEX IF NOT EXISTS ix_holds_open ON ledger_holds(subject) WHERE state = 'open';

-- Claim-first ledger. payload_hash makes "same key, different request" a loud
-- conflict instead of a silent overwrite. applied_unknown marks a claim whose
-- effect happened out of band — those are never auto-retried or auto-cleaned.
CREATE TABLE IF NOT EXISTS idempotency (
    key             TEXT    PRIMARY KEY,
    service         TEXT    NOT NULL,
    endpoint        TEXT    NOT NULL,
    payload_hash    TEXT    NOT NULL,
    state           TEXT    NOT NULL CHECK (state IN ('in_progress', 'done', 'failed')),
    applied_unknown INTEGER NOT NULL DEFAULT 0 CHECK (applied_unknown IN (0, 1)),
    response_json   TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- What happened, who did it, and how to undo it. money_coins is what the
-- system moved automatically; manual_coins is what a human still owes by hand.
-- They are separate columns because a confirm screen must show both.
CREATE TABLE IF NOT EXISTS audit_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL DEFAULT (datetime('now')),
    actor        TEXT    NOT NULL,
    -- who/what this action moved money against or affected -- 'order:12',
    -- 'pred_market:3', 'game_round:round.coinflip:...'. Free text, always
    -- given: CONTRACT.md sec 4 promises "who did it", but a row with only
    -- an actor and no target cannot say WHOM it was done to.
    target       TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    summary      TEXT    NOT NULL,
    money_coins  INTEGER NOT NULL DEFAULT 0,
    manual_coins INTEGER NOT NULL DEFAULT 0,
    ops_json     TEXT    NOT NULL,
    reversed_at  TEXT,
    action_key   TEXT    UNIQUE
);

-- ---------------------------------------------------------------- shop

-- A category can exist before it has any items -- it is how a PLANNED
-- section (e.g. "Ores", not stocked yet) gets recorded as a to-do rather
-- than invented ad hoc the day the first item in it is added. sort_order is
-- the shop-sheet order, not alphabetical, because that is how a shop sign
-- is actually read. Whether an empty category is worth SHOWING is a
-- display-layer decision, not a schema one: the public storefront hides a
-- category with no active items (an empty heading tells a customer
-- nothing); staff pages (/ledger, /admin) show it anyway, because there the
-- emptiness itself is the information -- it is the to-do list.
CREATE TABLE IF NOT EXISTS categories (
    name       TEXT    PRIMARY KEY,
    sort_order INTEGER NOT NULL,
    note       TEXT
);


-- price_stack_coins is the number the owner actually typed. It is never
-- divided at rest. Per-piece figures are derived at display and at charge
-- time by core.pricing.charge(), with one rounding rule in one place.
--
-- The last CHECK is the 64x bug killed at the schema: a non-stackable item
-- with stack_size 64 throws capacity, fullness and restock quantity off by
-- 64x, and it is not possible to write that row here.
CREATE TABLE IF NOT EXISTS items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    -- price_coins is the number the owner typed. price_unit_pieces is how many
    -- pieces that number buys: 64 for "1g/stack", 32 for "1g/32", 1 for
    -- "3g/each". stack_size is the MINECRAFT stack size and is used only for
    -- capacity and barrel maths.
    --
    -- These were one column and it was the same bug as the 64x one wearing a
    -- different hat: saplings stack to 64 but sell per 32, so a single column
    -- had to be wrong about one of the two. A price divided by the wrong
    -- number is a silent 2x, and nothing in the row says which meaning was
    -- intended.
    price_coins       INTEGER NOT NULL CHECK (price_coins >= 0),
    price_unit_pieces INTEGER NOT NULL DEFAULT 64 CHECK (price_unit_pieces > 0),
    stack_size        INTEGER NOT NULL DEFAULT 64 CHECK (stack_size > 0),
    stackable         INTEGER NOT NULL DEFAULT 1 CHECK (stackable IN (0, 1)),
    barrel_slots      INTEGER NOT NULL DEFAULT 54 CHECK (barrel_slots > 0),
    -- Shop-sheet grouping, the shelf space it occupies, and where the goods
    -- actually come from ("wither tree farm"). The supply note is the owner's
    -- own words and is shown to staff, never invented.
    category          TEXT,
    subcategory       TEXT,
    sort_order        INTEGER NOT NULL DEFAULT 0,
    slots             INTEGER CHECK (slots IS NULL OR slots > 0),
    supply_source     TEXT,
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (stackable = 1 OR stack_size = 1),
    CHECK (price_unit_pieces <= stack_size)
);
CREATE INDEX IF NOT EXISTS ix_items_category ON items(category, sort_order, subcategory, name);

CREATE TABLE IF NOT EXISTS stock (
    item_id    INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    pieces     INTEGER NOT NULL DEFAULT 0 CHECK (pieces >= 0),
    capacity   INTEGER NOT NULL DEFAULT 0 CHECK (capacity >= 0),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- acked_until_qty is the whole anti-spam mechanism. Acknowledge writes the
-- current quantity; the alert fires again only if things get WORSE than that,
-- and the row resets once stock climbs back over the threshold. No timers,
-- no cron state, nothing to drift.
CREATE TABLE IF NOT EXISTS stock_alerts (
    item_id          INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    threshold_pct    INTEGER CHECK (threshold_pct IS NULL OR (threshold_pct > 0 AND threshold_pct <= 100)),
    threshold_pieces INTEGER CHECK (threshold_pieces IS NULL OR threshold_pieces > 0),
    acked_until_qty  INTEGER,
    last_fired_at    TEXT,
    CHECK (threshold_pct IS NOT NULL OR threshold_pieces IS NOT NULL)
);

-- price_stack_coins and stack_size are SNAPSHOTTED at creation. Repricing an
-- item must never silently reprice work already in flight, and a repair tool
-- must never be able to price a cancelled order.
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id           INTEGER NOT NULL REFERENCES items(id),
    requested_pieces  INTEGER NOT NULL CHECK (requested_pieces > 0),
    produced_pieces   INTEGER NOT NULL DEFAULT 0 CHECK (produced_pieces >= 0),
    status            TEXT    NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open', 'claimed', 'awaiting_verification',
                                                'fulfilled', 'cancelled')),
    price_coins       INTEGER NOT NULL CHECK (price_coins >= 0),
    price_unit_pieces INTEGER NOT NULL CHECK (price_unit_pieces > 0),
    stack_size        INTEGER NOT NULL CHECK (stack_size > 0),
    created_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at         TEXT,
    channel_id        TEXT,
    message_id        TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_open ON orders(status) WHERE status IN ('open', 'claimed');
-- A persistent view's button re-resolves its order from the message it is on.
CREATE INDEX IF NOT EXISTS ix_orders_message ON orders(message_id);

-- paid_event is UNIQUE and set exactly once, in the same statement that pays.
-- That single constraint is the double-pay guard: a second attempt cannot
-- write the row, so it cannot pay.
CREATE TABLE IF NOT EXISTS order_claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    worker     TEXT    NOT NULL,
    pieces     INTEGER NOT NULL CHECK (pieces > 0),
    delivered  INTEGER NOT NULL DEFAULT 0 CHECK (delivered >= 0),
    paid_event TEXT    UNIQUE,
    paid_coins INTEGER CHECK (paid_coins IS NULL OR paid_coins >= 0),
    claimed_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (order_id, worker),
    CHECK ((paid_event IS NULL) = (paid_coins IS NULL))
);

-- Short codes so nobody ever types an id. Alphabet excludes l, o, 0 and 1 --
-- those are the glyphs people misread out of a chat window.
CREATE TABLE IF NOT EXISTS addresses (
    code       TEXT PRIMARY KEY CHECK (length(code) = 4),
    kind       TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, entity_id)
);

-- Public open-bid auctions on catalog items. Money-only: this table never
-- touches `stock` or `items.active` -- handing over the physical lot is a
-- staff task, same as an order's delivery. Not wagering (see
-- core/auctions.py's module docstring), so it lives here in the shop
-- section, not below the betting boundary, and core/wagering.py's guard
-- never sees it.
CREATE TABLE IF NOT EXISTS auctions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id),
    pieces          INTEGER NOT NULL CHECK (pieces > 0),
    min_bid         INTEGER NOT NULL CHECK (min_bid > 0),
    min_increment   INTEGER NOT NULL CHECK (min_increment > 0),
    status          TEXT    NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'closed', 'settled', 'voided')),
    winner          TEXT,
    winning_amount  INTEGER CHECK (winning_amount IS NULL OR winning_amount >= 0),
    settle_event    TEXT    UNIQUE,
    created_by      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    closes_at       TEXT    NOT NULL,
    settled_at      TEXT,
    channel_id      TEXT,
    message_id      TEXT,
    CHECK ((winner IS NULL) = (winning_amount IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_auctions_open ON auctions(status) WHERE status IN ('open', 'closed');
-- A persistent card's button re-resolves its auction from the message it is on.
CREATE INDEX IF NOT EXISTS ix_auctions_message ON auctions(message_id);

-- At most one 'active' row per auction at any time -- bid() marks the
-- previous leader 'outbid' (and releases its hold) in the SAME transaction
-- that inserts a new leader, so this invariant never needs a query to hold.
CREATE TABLE IF NOT EXISTS auction_bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id  INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    subject     TEXT    NOT NULL REFERENCES wallets(subject),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    hold_id     TEXT    NOT NULL REFERENCES ledger_holds(id),
    status      TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'outbid', 'won', 'refunded')),
    placed_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_auction_bids_auction ON auction_bids(auction_id, amount DESC);

-- Land plot listings. Staff lists a plot by hand (name, description, a
-- free-text location -- no chunk-claim-mod integration, no AI valuation:
-- see CONTRACT.md section 11a) and buyers bid, exactly like `auctions`
-- above, or settle it instantly with `buy_now_price` when one is set. Same
-- money-only contract as auctions: this table never touches `stock` or
-- `items` -- handing over the plot in-game is a staff task, same as an
-- order's delivery or a won item auction's lot.
CREATE TABLE IF NOT EXISTS land_listings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    location        TEXT    NOT NULL DEFAULT '',
    min_bid         INTEGER NOT NULL CHECK (min_bid > 0),
    min_increment   INTEGER NOT NULL CHECK (min_increment > 0),
    buy_now_price   INTEGER CHECK (buy_now_price IS NULL OR buy_now_price >= min_bid),
    status          TEXT    NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'closed', 'settled', 'voided')),
    winner          TEXT,
    winning_amount  INTEGER CHECK (winning_amount IS NULL OR winning_amount >= 0),
    settle_event    TEXT    UNIQUE,
    created_by      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    closes_at       TEXT    NOT NULL,
    settled_at      TEXT,
    channel_id      TEXT,
    message_id      TEXT,
    CHECK ((winner IS NULL) = (winning_amount IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_land_listings_open ON land_listings(status) WHERE status IN ('open', 'closed');
-- A persistent card's button re-resolves its listing from the message it is on.
CREATE INDEX IF NOT EXISTS ix_land_listings_message ON land_listings(message_id);

-- At most one 'active' row per listing at any time -- same invariant as
-- auction_bids, held the same way: bid() marks the previous leader
-- 'outbid' (and releases its hold) in the SAME transaction that inserts
-- the new leader.
CREATE TABLE IF NOT EXISTS land_bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    land_id     INTEGER NOT NULL REFERENCES land_listings(id) ON DELETE CASCADE,
    subject     TEXT    NOT NULL REFERENCES wallets(subject),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    hold_id     TEXT    NOT NULL REFERENCES ledger_holds(id),
    status      TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'outbid', 'won', 'refunded')),
    placed_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_land_bids_land ON land_bids(land_id, amount DESC);

-- ---------------------------------------------------------------- betting
-- Discord only. No module under web/ may read anything below this line;
-- tests/test_no_wagering_on_web.py fails the build if one does.

CREATE TABLE IF NOT EXISTS pred_markets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT    NOT NULL CHECK (length(trim(question)) > 0),
    status        TEXT    NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'closed', 'resolved', 'voided')),
    rake_bps      INTEGER NOT NULL DEFAULT 0 CHECK (rake_bps >= 0 AND rake_bps <= 1000),
    closes_at     TEXT,
    resolved_outcome_id INTEGER,
    resolve_event TEXT    UNIQUE,
    created_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    -- resolved means resolved: an outcome and an event id, or neither.
    CHECK ((status = 'resolved') = (resolved_outcome_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS pred_outcomes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL REFERENCES pred_markets(id) ON DELETE CASCADE,
    label     TEXT    NOT NULL CHECK (length(trim(label)) > 0),
    UNIQUE (market_id, label)
);

CREATE TABLE IF NOT EXISTS pred_stakes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     INTEGER NOT NULL REFERENCES pred_markets(id) ON DELETE CASCADE,
    outcome_id    INTEGER NOT NULL REFERENCES pred_outcomes(id),
    subject       TEXT    NOT NULL REFERENCES wallets(subject),
    amount        INTEGER NOT NULL CHECK (amount > 0),
    hold_id       TEXT    NOT NULL REFERENCES ledger_holds(id),
    settled_event TEXT    UNIQUE,
    payout_coins  INTEGER CHECK (payout_coins IS NULL OR payout_coins >= 0),
    placed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK ((settled_event IS NULL) = (payout_coins IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_stakes_market ON pred_stakes(market_id, outcome_id);

-- server_seed_hash is committed before the round; server_seed is NULL until
-- settlement reveals it. Anyone can recompute the outcome afterwards.
CREATE TABLE IF NOT EXISTS game_rounds (
    id               TEXT    PRIMARY KEY,
    game             TEXT    NOT NULL CHECK (game IN ('coinflip', 'dice', 'slots')),
    server_seed_hash TEXT    NOT NULL,
    server_seed      TEXT,
    client_seed      TEXT    NOT NULL,
    nonce            INTEGER NOT NULL,
    outcome_json     TEXT,
    state            TEXT    NOT NULL DEFAULT 'open'
                             CHECK (state IN ('open', 'settled', 'voided')),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at       TEXT,
    -- a settled round has revealed its seed and its outcome, or it is not settled
    CHECK ((state = 'settled') = (server_seed IS NOT NULL AND outcome_json IS NOT NULL))
);

-- A commitment is published (hash only) BEFORE any stake is accepted, and the
-- seed behind it is random -- never derived from the round id, the player or
-- the bet. `next_nonce` is claimed atomically per commitment, so a nonce is
-- never reused and never rewound. Revealing happens at settlement.
CREATE TABLE IF NOT EXISTS game_commitments (
    id               TEXT    PRIMARY KEY,
    server_seed      TEXT    NOT NULL,
    server_seed_hash TEXT    NOT NULL,
    next_nonce       INTEGER NOT NULL DEFAULT 0 CHECK (next_nonce >= 0),
    state            TEXT    NOT NULL DEFAULT 'open'
                             CHECK (state IN ('open', 'revealed')),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    revealed_at      TEXT
);

CREATE TABLE IF NOT EXISTS game_bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id      TEXT    NOT NULL REFERENCES game_rounds(id) ON DELETE CASCADE,
    subject       TEXT    NOT NULL REFERENCES wallets(subject),
    amount        INTEGER NOT NULL CHECK (amount > 0),
    selection     TEXT    NOT NULL,
    hold_id       TEXT    NOT NULL REFERENCES ledger_holds(id),
    payout_coins  INTEGER CHECK (payout_coins IS NULL OR payout_coins >= 0),
    settled_event TEXT    UNIQUE,
    placed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK ((settled_event IS NULL) = (payout_coins IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_bets_round ON game_bets(round_id);

-- Per-day staked/lost totals, so MAX_DAILY_LOSS is enforceable in the same
-- statement that places the bet rather than by a read-then-decide.
CREATE TABLE IF NOT EXISTS gambling_day (
    subject TEXT    NOT NULL REFERENCES wallets(subject) ON DELETE CASCADE,
    day     TEXT    NOT NULL,
    staked  INTEGER NOT NULL DEFAULT 0 CHECK (staked >= 0),
    lost    INTEGER NOT NULL DEFAULT 0 CHECK (lost >= 0),
    PRIMARY KEY (subject, day)
);

-- ---------------------------------------------------------------- plumbing

CREATE TABLE IF NOT EXISTS config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What `/setup` built, so it never builds it twice. `key` is OUR name for a
-- thing ('channel:shop', 'role:staff'); discord_id is what Discord called it.
--
-- This table is why the three channel ids are no longer required env vars: a
-- fresh server has no channels to put in a .env, and a panel-only host has no
-- shell to add them from afterwards. The bot provisions its own layout and
-- remembers what it made.
--
-- `name` is the name at creation time, kept only for diagnostics -- people
-- rename channels and that must not orphan anything. The id is the identity.
CREATE TABLE IF NOT EXISTS guild_layout (
    guild_id   INTEGER NOT NULL,
    key        TEXT NOT NULL,
    discord_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token      TEXT PRIMARY KEY,
    subject    TEXT NOT NULL REFERENCES wallets(subject) ON DELETE CASCADE,
    name       TEXT,
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON web_sessions(expires_at);

-- ---------------------------------------------------------------------------
-- Reference market (CONTRACT.md section 13)
--
-- A read-only mirror of somebody else's public market, kept so the owner can
-- see what an item fetches elsewhere before he prices it here. NOTHING in
-- this file is money and nothing here may ever be written into `items` by a
-- machine: the prices are in a FOREIGN currency on a FOREIGN server, so the
-- figures are not comparable to `g` in absolute terms and only the shape --
-- what is dear there and cheap here -- carries information. That is why
-- `price` is REAL here while every coin in this database is an integer: this
-- column is a quoted observation, not a balance.
--
-- One row per (source, item_key), overwritten each cycle. History is not kept
-- because the source publishes its own 7-day trend and storing 765 rows every
-- six hours forever would cost more than it tells anyone.
CREATE TABLE IF NOT EXISTS ref_market (
    source        TEXT NOT NULL,
    item_key      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    -- The normalised name used to line their catalogue up with ours:
    -- lowercase, letters and digits only. 'Smooth Stone' and 'SMOOTH_STONE'
    -- both become 'smoothstone'. Stored rather than computed at read time so
    -- the join can use an index and so a bad normalisation is visible in the
    -- table instead of hiding inside a query.
    match_name    TEXT NOT NULL,
    price         REAL,          -- their price, per piece, their currency
    price_source  TEXT,          -- how they derived it ('trades_24h', ...)
    best_ask      REAL,          -- cheapest thing actually for sale, per piece
    best_bid      REAL,          -- best standing offer to buy, per piece
    stock         INTEGER,       -- pieces on offer there
    demand        INTEGER,       -- pieces wanted there -- the number he asked for
    volume_24h    INTEGER,       -- pieces traded there in 24h
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_key)
);
CREATE INDEX IF NOT EXISTS ix_ref_market_match ON ref_market(match_name);

-- One row per source. Exists because "the table has rows" and "the pull is
-- still working" are different facts, and a dead feed that still has last
-- week's numbers in it looks exactly like a healthy one. Every field here is
-- about the LAST ATTEMPT, not the last success, except ok_at.
CREATE TABLE IF NOT EXISTS ref_market_runs (
    source        TEXT PRIMARY KEY,
    ok_at         TEXT,          -- last cycle that actually stored rows
    attempted_at  TEXT,          -- last cycle that ran at all
    rows          INTEGER NOT NULL DEFAULT 0,
    error         TEXT           -- NULL when the last attempt succeeded
);
